#!/usr/bin/env python3
"""Builds, tests, and publishes the latest versions of the Oasis toolchain."""

import collections
from contextlib import contextmanager
from datetime import datetime
import json
import os
import os.path as osp
import subprocess
from subprocess import PIPE, DEVNULL
import sys

import boto3

ToolSpec = collections.namedtuple('ToolSpec', 'pkg source envs s3_key')

BASE_DIR = osp.abspath(osp.dirname(__file__))
TOOLS_DIR = osp.join(BASE_DIR, 'tools')
BIN_DIR = osp.join(TOOLS_DIR, 'bin')
CANARIES_DIR = osp.join(BASE_DIR, 'canaries')
MYPROJ = 'my_project'
BIN_BUCKET = 'tools.oasis.dev'
CACHE_BIN_PFX = f'{sys.platform}/cache/'
CD_BIN_PFX = f'{sys.platform}/current/'  # cd = continuous deployment
HISTFILE_KEY = 'successful_builds'


def main():
    with open('config.json') as f_config:
        config = json.load(f_config)

    toolspecs = get_toolspecs(config)
    tool_hashes = frozenset(spec.s3_key for spec in toolspecs.values())

    s3 = boto3.client('s3')

    _, last_hashes = get_history(s3)
    if tool_hashes == last_hashes:
        print(f'current: {" ".join(last_hashes)}')
        return

    get_tools(toolspecs, s3)
    run_tests(config)
    update_toolstate(toolspecs, s3)


def get_tools(toolspecs, s3):
    """Fetches current tools from the S3 cache or builds them locally.
    Built tools that are not in the cache are added. Stale tools are removed."""
    os.makedirs(BIN_DIR, exist_ok=True)
    already_cached = get_tool_keys(s3, prefix=CACHE_BIN_PFX)
    for tool, spec in toolspecs.items():
        bin_file = osp.join(BIN_DIR, tool)
        cache_key = f'{CACHE_BIN_PFX}{spec.s3_key}'

        if spec.s3_key in already_cached:
            s3.download_file(BIN_BUCKET, cache_key, bin_file)
            print(f'+ aws s3 cp s3://{BIN_BUCKET}/{cache_key} {osp.relpath(bin_file, os.getcwd())}')
            del already_cached[spec.s3_key]  # not stale
        else:
            envs = dict(**spec.envs, CARGO_TARGET_DIR=osp.join(TOOLS_DIR, 'target'))
            run(f'cargo install --force -q --root {TOOLS_DIR} --git {spec.source} {spec.pkg}',
                envs=envs)
            s3.upload_file(bin_file, BIN_BUCKET, cache_key)
    run(f'chmod -R a+x {TOOLS_DIR}')

    if already_cached:  # remaining keys are stale
        to_delete = {'Objects': [{'Key': k} for k in already_cached.values()]}
        s3.delete_objects(Bucket=BIN_BUCKET, Delete=to_delete)


def get_tool_keys(s3, prefix):
    """Returns an object containing {tool_name-hash: object_s3_key}"""
    objs = s3.list_objects_v2(Bucket=BIN_BUCKET, Prefix=prefix)
    tool_keys = {}
    for obj in objs.get('Contents', []):
        key = obj['Key']
        tool_keys[key.rsplit('/', 1)[-1]] = key  # `<platform>/<prefix>/tool_name-hash`
    return tool_keys


def run_tests(config):
    """Builds, unit tests, and locally deploys all projects found in canary repos
       and the starter repo produced by `oasis init`."""

    run('oasis', input='y\n', stdout=DEVNULL, check=False)  # gen config if needed

    with oasis_chain():
        for canary in config['canaries']:
            canary_dir = osp.split(canary)[-1]
            with pushd(osp.join(CANARIES_DIR, canary_dir)):
                if osp.isdir('.git'):
                    run('git fetch origin && git reset --hard origin/master',
                        stdout=DEVNULL,
                        stderr=DEVNULL)
                else:
                    run(f'git clone -q --depth 1 https://github.com/{canary} .', stdout=DEVNULL)

                # Build services before apps. Each is done individually because a canary
                # repo might contain multiple projects.
                for service_manifest in find_manifests('Cargo.toml'):
                    with pushd(osp.dirname(service_manifest)):
                        run('oasis build -q')
                        run('oasis test -q')

                for app_manifest in find_manifests('package.json'):
                    with pushd(osp.dirname(app_manifest)):
                        run('yarn install -s')
                        run('oasis test -q')

        myproj_dir = osp.join(CANARIES_DIR, MYPROJ)
        run(f'rm -rf {myproj_dir} && oasis init -qq {myproj_dir}')
        with pushd(myproj_dir):
            # In the quickstart, we can assume that the root contains only one project.
            run('oasis build -q')
            run('oasis test -q')


def update_toolstate(toolspecs, s3):
    """Uploads built artifacts to the s3 under the current-but-not-released prefex.
       Removes any outdated artifacts."""
    tstamp = datetime.utcnow().isoformat()
    artifact_keys = (spec.s3_key for spec in toolspecs.values())
    history, _ = get_history(s3)
    # ^ history has to be re-fetched so that it's as close to atomic as possible.
    # remember that other platforms' builds are also trying to push.
    history.append(f'{tstamp} {sys.platform} {" ".join(artifact_keys)}')
    s3.put_object(
        Bucket=BIN_BUCKET,
        Key=HISTFILE_KEY,
        Body='\n'.join(history).encode('utf8'),
        ACL='public-read')

    existing_cd_keys = get_tool_keys(s3, CD_BIN_PFX)
    for spec in toolspecs.values():
        new_cache_key = f'{CACHE_BIN_PFX}{spec.s3_key}'
        new_cd_key = f'{CD_BIN_PFX}{spec.s3_key}'
        if spec.s3_key in existing_cd_keys:
            del existing_cd_keys[spec.s3_key]
        else:
            src = dict(Bucket=BIN_BUCKET, Key=new_cache_key)
            s3.copy(src, BIN_BUCKET, new_cd_key)

    if existing_cd_keys:
        to_delete = {'Objects': [{'Key': k} for k in existing_cd_keys.values()]}
        s3.delete_objects(Bucket=BIN_BUCKET, Delete=to_delete)


def run(cmd, envs=None, check=True, **run_args):
    penvs = dict(os.environ)
    penvs['PATH'] = BIN_DIR + ':' + penvs['PATH']
    if envs:
        penvs.update(envs)
    print(f'+ {cmd}')
    return subprocess.run(cmd, shell=True, env=penvs, check=check, encoding='utf8', **run_args)


def get_toolspecs(config):
    """Returns an object containing {tool_name: ToolSpec}"""
    specs = {}
    for tool, cfg in config['tools'].items():
        cfg = cfg if cfg else {}
        pkg = cfg.get('pkg', tool)
        source = cfg.get('source', f'https://github.com/oasislabs/{pkg}')
        envs = cfg.get('envs', {})
        hash_ = run(f'git ls-remote {source} master | cut -f1', stdout=PIPE).stdout[:7]
        specs[tool] = ToolSpec(source=source, envs=envs, pkg=pkg, s3_key=f'{tool}-{hash_}')
    return specs


def get_history(s3):
    """Returns:
        - lines of the histfile
        - the hashes of the last successfully built tools for this platform"""
    # see `update_toolstate` for the format of the histfile
    try:
        history_obj = s3.get_object(Bucket=BIN_BUCKET, Key=HISTFILE_KEY)
        body = history_obj['Body'].read().decode('utf8')
        history = body.split('\n')
    except s3.exceptions.NoSuchKey:
        history = []
    last_hashes = frozenset()
    for build_stats in history[::-1]:
        _date, platform, *tool_hashes = build_stats.split(' ')
        if platform != sys.platform:
            continue
        last_hashes = frozenset(tool_hashes)
        break
    return (history, last_hashes)


def find_manifests(*names):
    """Returns the paths of all files in `names` in the repo containing cwd."""
    names_alt = '\\|'.join(names)
    return run(f'git ls-files | grep -e "{names_alt}"', stdout=PIPE).stdout.split()


@contextmanager
def pushd(path):
    orig_dir = os.getcwd()
    os.makedirs(path, exist_ok=True)
    os.chdir(path)
    print(f'+ cd {osp.relpath(os.getcwd(), orig_dir)}')
    yield
    print(f'+ cd {osp.relpath(orig_dir, os.getcwd())}')
    os.chdir(orig_dir)


@contextmanager
def oasis_chain():
    env = {'PATH': f'{BIN_DIR}:/usr/bin', 'HOME': os.environ['HOME']}
    cp = subprocess.Popen(['oasis', 'chain'], env=env, stdout=DEVNULL)
    yield
    cp.terminate()
    cp.wait()


if __name__ == '__main__':
    main()
