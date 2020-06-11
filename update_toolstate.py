#!/usr/bin/env python3
"""Builds, tests, and publishes the latest versions of the Oasis toolchain."""

from collections import namedtuple
from contextlib import contextmanager
import os
import os.path as osp
import shutil
import subprocess
from subprocess import PIPE, DEVNULL
import sys

import boto3
import schema
import yaml

BASE_DIR = osp.abspath(osp.dirname(__file__))
TOOLS_DIR = osp.join(BASE_DIR, "tools")
BIN_DIR = osp.join(TOOLS_DIR, "bin")
CANARIES_DIR = osp.join(BASE_DIR, "canaries")
MYPROJ = "my_project"
BIN_BUCKET = "tools.oasis.dev"
CACHE_BIN_PFX = f"{sys.platform}/cache/"
CD_BIN_PFX = f"{sys.platform}/current/"  # cd = continuous deployment


class Config:
    """Tools config object."""

    GITHUB_REPO_RE = schema.Regex(r"\w+/\w+")
    CONFIG_SCHEMA = schema.Schema(
        {
            "tools": {
                str: {"source": GITHUB_REPO_RE, schema.Optional("builder", default=None): str}
            },
            "canaries": [GITHUB_REPO_RE],
        }
    )

    Tool = namedtuple("Tool", "name source builder")

    def __init__(self, config_obj):
        config = self.CONFIG_SCHEMA.validate(config_obj)
        self.tools = {
            name: self.Tool(name, self._fmt_github_url(spec["source"]), spec["builder"])
            for name, spec in config["tools"].items()
        }
        self.canaries = list(map(self._fmt_github_url, config_obj["canaries"]))

    @staticmethod
    def _fmt_github_url(owner_repo):
        return f"https://github.com/{owner_repo}"

    def sources(self):
        return {t.source for t in self.tools.values()}


def main():
    with open("config.yml") as f_config:
        config = Config(yaml.safe_load(f_config))

    with s3_client() as s3:
        head_versions = get_head_versions(config)
        cached_versions = get_cached_versions(s3)

    to_build = {}  # name: ver
    for tool, cur_ver in head_versions.items():
        if cached_versions.get(tool) != cur_ver:
            to_build[tool] = cur_ver

    if not to_build:
        print(f"current: {' '.join('-'.join(name_ver) for name_ver in head_versions.items())}")
        return

    update_current = False
    try:
        new_tools = [(config.tools[name], ver) for name, ver in to_build.items()]
        build_tools(new_tools)
        # run_tests(config)
        update_current = True
    finally:
        with s3_client() as s3:
            sync_tools(head_versions, cached_versions, update_current, s3)


def build_tools(tool_vers):
    """Builds new tools."""
    shutil.rmtree(BIN_DIR, ignore_errors=True)
    os.makedirs(BIN_DIR)
    for (tool, ver) in tool_vers:
        repo_dir = osp.join(TOOLS_DIR, tool.source.rsplit("/", 1)[-1])
        if not osp.isdir(repo_dir):
            run(f"git clone -q {tool.source} {repo_dir}")
        with pushd(repo_dir):
            run(f"git fetch origin && git checkout -q {ver}")
            if tool.builder is not None:
                run(tool.builder)
                shutil.copy(tool.name, BIN_DIR)
            elif osp.isfile("Cargo.toml"):
                run(f"cargo build -q --locked --release --bin {tool.name}",)
                shutil.copy(osp.join("target", "release", tool.name), BIN_DIR)
            elif osp.isfile("go.mod"):
                raise RuntimeError("auto go build are not yet supported. please specify `builder`")
            else:
                raise RuntimeError("unable to auto-detect project type")


def run_tests(config):
    # pylint: disable=unused-variable
    """Builds, unit tests, and locally deploys all projects found in canary repos
       and the starter repo produced by `oasis init`."""

    canaries = config.get("canaries", [])
    if not canaries:
        return

    run("oasis", input="y\n", stdout=DEVNULL, check=False)  # gen config if needed

    with oasis_chain():
        for canary in canaries:
            canary_dir = osp.split(canary)[-1]
            with pushd(osp.join(CANARIES_DIR, canary_dir)):
                if osp.isdir(".git"):
                    run(
                        "git fetch origin && git reset --hard origin/master",
                        stdout=DEVNULL,
                        stderr=DEVNULL,
                    )
                else:
                    run(
                        f"git clone -q --depth 1 https://github.com/{canary} .", stdout=DEVNULL,
                    )

                # Build services before apps. Each is done individually because a canary
                # repo might contain multiple projects.
                for service_manifest in find_manifests("Cargo.toml"):
                    with pushd(osp.dirname(service_manifest)):
                        run("oasis build -q")
                        run("oasis test -q")

                for app_manifest in find_manifests("package.json"):
                    with pushd(osp.dirname(app_manifest)):
                        run("yarn install -s")
                        run("oasis test -q")

        myproj_dir = osp.join(CANARIES_DIR, MYPROJ)
        run(f"rm -rf {myproj_dir} && oasis init -qq {myproj_dir}")
        with pushd(myproj_dir):
            # In the quickstart, we can assume that the root contains only one project.
            run("oasis build -q")
            run("oasis test -q")


def find_manifests(*names):
    """Returns the paths of all files in `names` in the repo containing cwd."""
    names_alt = "\\|".join(names)
    return run(f'git ls-files | grep -e "{names_alt}"', stdout=PIPE).stdout.split()


def sync_tools(head_versions, cached_versions, update_current, s3):
    """Uploads built artifacts to the s3 under the current-but-not-released prefex.
       Removes any outdated artifacts."""
    current_versions = get_current_versions(s3)
    built_tools = {de.name for de in os.scandir(BIN_DIR)}

    to_delete = []

    def _upload_tool(prefix, tool):
        s3.upload_file(
            osp.join(BIN_DIR, tool), BIN_BUCKET, get_s3_key(prefix, tool, head_versions[tool]),
        )

    for tool in built_tools:
        _upload_tool(CACHE_BIN_PFX, tool)
        cached_version = cached_versions.get(tool)
        if cached_version:
            to_delete.append(get_s3_key(CACHE_BIN_PFX, tool, cached_version))

    if update_current:
        to_delete.extend(
            get_s3_key(CD_BIN_PFX, tool, ver)
            for tool, ver in current_versions.items()
            if head_versions.get(tool) != ver
        )
        for tool, ver in head_versions.items():
            cache_key = get_s3_key(CACHE_BIN_PFX, tool, ver)
            cd_key = get_s3_key(CD_BIN_PFX, tool, ver)
            s3.copy_object(
                Bucket=BIN_BUCKET, Key=cd_key, CopySource=dict(Bucket=BIN_BUCKET, Key=cache_key)
            )
        for tool in built_tools:
            _upload_tool(CD_BIN_PFX, tool)

    if to_delete:
        s3.delete_objects(Bucket=BIN_BUCKET, Delete={"Objects": [{"Key": k} for k in to_delete]})


def get_head_versions(config):
    """Returns { <tool-name>: <git-rev> } """
    source_revs = {
        source: run(f"git ls-remote {source} master | cut -f1", stdout=PIPE).stdout[:7]
        for source in config.sources()
    }
    return {t.name: source_revs[t.source] for t in config.tools.values()}


def get_current_versions(s3):
    """Returns the current tools as `{ <tool name>: <version> }`."""
    return _get_tools_in(s3, CD_BIN_PFX)


def get_cached_versions(s3):
    """Returns the cached tools as `{ <tool name>: <version> }`."""
    return _get_tools_in(s3, CACHE_BIN_PFX)


def _get_tools_in(s3, prefix):
    """Returns the `{ <tool name>: <version> }`s in the bucket under `prefix`."""
    objs = s3.list_objects_v2(Bucket=BIN_BUCKET, Prefix=prefix).get("Contents", [])
    return dict(parse_s3_key(obj["Key"]) for obj in objs)


def get_s3_key(prefix, tool, version):
    """Returns the key for the tool and version under the provided prefix."""
    return osp.join(prefix, f"{tool}-{version}")


def parse_s3_key(key):
    """Parses the S3 object path into (name, ver)."""
    return key.rsplit("/", 1)[-1].rsplit("-", 1)


def run(cmd, envs=None, check=True, **run_args):
    penvs = dict(os.environ)
    penvs["PATH"] = BIN_DIR + ":" + penvs["PATH"]
    if envs:
        penvs.update(envs)
    print(f"+ {cmd}")
    return subprocess.run(cmd, shell=True, env=penvs, check=check, encoding="utf8", **run_args)


@contextmanager
def s3_client():
    """Yields a boto s3 client that has permissions to modify the toolstate bucket."""
    aws_cred_names = ["aws_access_key_id", "aws_secret_access_key", "aws_session_token"]
    aws_creds = run(".github/workflows/get-s3-creds.sh", stdout=PIPE).stdout.split("\t")
    yield boto3.client("s3", **dict(zip(aws_cred_names, aws_creds)))


@contextmanager
def pushd(path):
    orig_dir = os.getcwd()
    os.makedirs(path, exist_ok=True)
    os.chdir(path)
    print(f"+ cd {osp.relpath(os.getcwd(), orig_dir)}")
    try:
        yield
    finally:
        print(f"+ cd {osp.relpath(orig_dir, os.getcwd())}")
        os.chdir(orig_dir)


@contextmanager
def oasis_chain():
    env = {"PATH": f"{BIN_DIR}:/usr/bin", "HOME": os.environ["HOME"]}
    cp = subprocess.Popen(["oasis", "chain"], env=env, stdout=DEVNULL)
    yield
    cp.terminate()
    cp.wait()


if __name__ == "__main__":
    main()
