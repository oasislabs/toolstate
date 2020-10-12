#!/usr/bin/env python
"""Bootstraps the Oasis CLI and uses it to install the most recent release.
This script lives in https://github.com/oasislabs/toolstate.
"""

import argparse
import os
import os.path as osp
import platform
import re
import shlex
import subprocess
import sys

TOOLS_URL = "http://tools.oasis.dev.s3-us-west-2.amazonaws.com"
NODE_DIST_URL = "https://nodejs.org/dist/{ver}/node-{ver}-{plat}-x64.tar.gz"
RUST_VER = "nightly-2019-08-26"
REQUIRED_UTILS = ["cc", "ld", "curl", "git"]
# Library dependencies matrix.
REQUIRED_LIBS = {"linux": ["libssl.so.1.1", "libcrypto.so.1.1"], "darwin": []}
REQUIRED_NODE_VERSION = "12"

PLAT_DARWIN = "darwin"
PLAT_LINUX = "linux"
RUST_SYSROOT_PREFIX = "toolchains/%s-x86_64-" % RUST_VER
INSTALLED_DEPS_FILE = "installed_dependencies"
DEVNULL = open("/dev/null", "w")


def main():
    env_info = _get_env_info()

    if env_info.plat not in {PLAT_DARWIN, PLAT_LINUX} or platform.machine() != "x86_64":
        raise RuntimeError("Unsupported platform: %s (%s)" % (env_info.plat, platform.machine()))

    missing_utils = [u for u in REQUIRED_UTILS if not which(u)]
    if missing_utils:
        raise RuntimeError("Missing system utilities: %s" % ", ".join(missing_utils))

    missing_libs = [lib for lib in REQUIRED_LIBS[env_info.plat] if not installed_lib(lib)]
    if missing_libs:
        raise RuntimeError(
            "Missing %s system libraries: %s" % (env_info.plat, ", ".join(missing_libs))
        )

    args = _parse_args()

    install(args, env_info)

    has_oasis_on_path = which("oasis")
    if args.no_modify_shell and not has_oasis_on_path:
        required_exports = get_shell_additions(args, env_info)
        print_important("Remember to run the following before using `oasis`:\n")
        print_important("\n".join("    " + e for e in required_exports))
        print("")
    elif not has_oasis_on_path:
        rc_file = modify_shell_profile(args, env_info)
        print_info("`oasis` will be available when you next log in.\n")
        print_info("To configure your current shell run `source %s`.\n" % rc_file)

    print_success("You're ready to start developing on Oasis!")


def _parse_args():
    if "XDG_DATA_DIR" in os.environ:
        default_prefix = osp.dirname(os.environ["XDG_DATA_DIR"])
    else:
        default_prefix = osp.join(os.environ["HOME"], ".local")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--toolchain",
        default="latest",
        help="Which version of the Oasis toolchain to install. Default: latest",
    )
    parser.add_argument(
        "--prefix",
        default=default_prefix,
        help="Installation prefix. Default: `%s`" % default_prefix,
    )
    parser.add_argument(
        "--no-modify-shell", action="store_true", help="Don't modify your shell profile."
    )
    parser.add_argument(
        "--force", action="store_true", help="Force install of not-deselected components."
    )
    parser.add_argument(
        "--no-node", action="store_true", help="Don't install Node. Even if it's missing."
    )
    parser.add_argument(
        "--no-rust", action="store_true", help="Don't install Rust. Even if it's missing."
    )
    parser.add_argument(
        "--speedrun", action="store_true", help="Accept default options for all installed tools."
    )
    args = parser.parse_args()

    args.bin_dir = _ensure_dir(osp.join(args.prefix, "bin"))

    return args


def _get_env_info():
    home_dir = os.environ["HOME"]
    data_home = os.environ.get("XDG_DATA_HOME", osp.join(home_dir, ".local", "share"))

    return argparse.Namespace(
        home_dir=home_dir,
        data_dir=_ensure_dir(osp.join(data_home, "oasis")),
        rustup_home=os.environ.get("RUSTUP_HOME", osp.join(home_dir, ".rustup")),
        cargo_home=os.environ.get("CARGO_HOME", osp.join(home_dir, ".cargo")),
        shell=os.environ.get("SHELL", "sh"),
        plat=platform.system().lower(),
    )


def _ensure_dir(path):
    if osp.exists(path) and not osp.isdir(path):
        raise RuntimeError("`%s` is expected to be a directory." % path)
    if not osp.exists(path):
        os.makedirs(path)
    return path


def install(args, env_info):
    installed_deps_path = osp.join(env_info.data_dir, INSTALLED_DEPS_FILE)
    preinstalled_deps = set()
    if osp.isfile(installed_deps_path):
        with open(installed_deps_path) as f_installed:
            preinstalled_deps.update(f_installed.read().rstrip().split("\n"))
    else:
        with open(installed_deps_path, "w") as _:
            pass

    def _record_install(dep):
        """Record installed tool so that they can be uninstalled later."""
        if dep in preinstalled_deps:
            return
        with open(installed_deps_path, "a") as f_installed:
            f_installed.write(dep + "\n")

    has_rust_install = which("rustup")
    if not args.no_rust and (args.force or not has_rust_install):
        print_header("Installing Rust")
        install_rust()
        _record_install("rust")
        print("")
    if not args.no_rust:
        rustup_bin = osp.join(env_info.cargo_home, "bin", "rustup")
        run("%s toolchain install %s" % (rustup_bin, RUST_VER), silent=True)
        run("%s target add wasm32-wasi --toolchain %s" % (rustup_bin, RUST_VER), silent=True)

    def bin_dir(*x):
        return osp.join(args.bin_dir, *x)

    def get_node_version():
        """Returns the un-prefixed semver of the Node.js executable."""
        # Node executable on Ubuntu <=17.10 is named nodejs.
        for node_exe in ["node", "nodejs"]:
            # Node executables might be in ~/.local/bin but not on PATH.
            if not which(node_exe):
                node_exe = bin_dir(node_exe)
            if which(node_exe):
                # Trim-off leading "v".
                return run("%s --version" % node_exe, capture=True)[1:]

        return ""

    if not args.no_node:
        node_version = get_node_version()
        if node_version and not args.force and not semver_greater_or_equal(node_version,
                                                                           REQUIRED_NODE_VERSION):
            raise RuntimeError(
                "Node version %s found, but minimum required version is %s. Please remove \
locally installed node."
                % (node_version, REQUIRED_NODE_VERSION)
            )

        if args.force or not node_version:
            print_header("Installing Node")
            node_ver = install_node(args, env_info)
            _record_install("node-%s" % node_ver)
            print("")

    has_oasis_install = osp.isfile(bin_dir("oasis-chain")) and is_oasis(bin_dir("oasis"))
    if args.force or not has_oasis_install:
        print_header("Installing the Oasis toolchain...")
        install_oasis(args, env_info)
        print("")
    else:
        print_header("The Oasis toolchain is already installed.")
        print("Run `oasis set-toolchain latest` to update.\n")


def semver_greater_or_equal(installed_ver, required_ver):
    """Compares installed version with required version of the package in semver format.

    Semver is expected of format x.y.z-w+q. Compares installed x.y.z with required x.y.z.
    """

    def split_semver(ver):
        return list(map(int, ver.split("-")[0].split(".")))

    return split_semver(installed_ver) >= split_semver(required_ver)


def install_rust():
    rustup_args = "-y --no-modify-path --default-toolchain " + RUST_VER
    run(
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- %s" % rustup_args,
        shell=True,
    )  # curl invocation taken from https://rustup.rs


def install_node(args, env_info):
    node_vers = run("curl -sSL https://nodejs.org/dist/latest-v12.x/", capture=True)
    node_ver, node_major_ver = re.search(r"node-(v(\d+)\.\d+\.\d+)\.tar.gz", node_vers).groups()

    if env_info.plat == PLAT_DARWIN:
        if which("brew"):  # This will a.s. be Homebrew.
            if args.force:
                run("brew uninstall node", check=False, silent=True)
            return run("brew install %s node@12" % ("--force" if args.force else ""))

        if which("port"):  # There are no common non-MacPorts tools with this name.
            return run("port install node%s" % node_major_ver)

    curl = 'curl -L# "%s"' % NODE_DIST_URL.format(plat=env_info.plat, ver=node_ver)
    tar = 'tar xz -C %s --strip-components=1 --exclude "*.md" --exclude LICENSE' % args.prefix
    run("{curl} | {tar}".format(curl=curl, tar=tar), shell=True)

    return node_ver


def install_oasis(args, env_info):
    tools_xml = run("curl -sSL %s" % TOOLS_URL, capture=True)
    oasis_cli_key = re.search(r"%s/current/oasis-[0-9a-f]{7,}" % env_info.plat, tools_xml).group(0)

    oasis_path = osp.join(args.prefix, "bin", "oasis")
    if not args.force and osp.exists(oasis_path):
        raise RuntimeError("`%s` already exists!" % oasis_path)

    s3_url = "%s/%s" % (TOOLS_URL, oasis_cli_key)
    run("curl -sSLo {path} {url}".format(path=oasis_path, url=s3_url))
    run("chmod a+x %s" % oasis_path)
    if args.speedrun:
        oasis_cp = subprocess.Popen(
            oasis_path, stdin=subprocess.PIPE, stdout=DEVNULL, stderr=DEVNULL
        )
        oasis_cp.stdin.close()  # EOF will cause config prompt to use default options
        if oasis_cp.wait() != 0:
            print_error("Unable to set default configuration for Oasis CLI (--speedrun)")

    run("%s set-toolchain %s" % (oasis_path, args.toolchain), env=_skipconfig_env())


def _skipconfig_env():
    env = dict(os.environ)
    env["OASIS_SKIP_GENERATE_CONFIG"] = "1"
    return env


def is_oasis(path):
    """Returns whether the binary at `path` is the Oasis CLI."""
    if not osp.isfile(path) or osp.isdir(path):
        return False
    try:
        help_msg = run("%s --help" % path, capture=True, env=_skipconfig_env())
        return "Oasis developer tools" in help_msg
    except (OSError, subprocess.CalledProcessError):
        pass
    return False


def get_shell_additions(args, env_info):
    """Returns the env exports required to run the Oasis toolchain."""
    path_export = "export PATH=%s/bin:${CARGO_HOME:-~/.cargo}/bin:$PATH" % args.prefix
    exports = [path_export]
    ld_path_key = "%s_LIBRARY_PATH" % ("DYLD" if env_info.plat == PLAT_DARWIN else "LD")
    if osp.join(env_info.rustup_home, RUST_SYSROOT_PREFIX) not in os.environ.get(ld_path_key, ""):
        exports.append("export {0}=$(rustc --print sysroot)/lib:${0}".format(ld_path_key))

    data_dir = osp.join(args.prefix, "share", "oasis")
    if "zsh" in env_info.shell:
        exports.append('fpath=("%s" $fpath)' % data_dir)
    elif "bash" in env_info.shell:
        exports.append('source "%s"' % osp.join(data_dir, "completions.sh"))

    return exports


def modify_shell_profile(args, env_info):
    """Adds the Oasis tools to the user's PATH via a profile file.
       Assumes that the current shell is the user's preferred shell
       so to not pollute other shells' profiles."""
    if "zsh" in env_info.shell:
        rcfile = osp.join(os.environ.get("ZDOTDIR", "~"), ".zprofile")
    elif "bash" in env_info.shell:
        if env_info.plat == PLAT_DARWIN:
            rcfile = "~/.bash_profile"
        else:
            rcfile = "~/.bashrc"
    else:
        rcfile = "~/.profile"
    rc_file = osp.expanduser(rcfile)

    required_exports = get_shell_additions(args, env_info)

    rc_lines = set()
    if osp.isfile(rc_file):
        with open(rc_file) as f_rc:
            rc_lines = set(line.rstrip() for line in f_rc)

    if not all(export in rc_lines for export in required_exports):
        with open(rc_file, "a") as f_rc:
            f_rc.write("\n%s\n" % "\n".join(required_exports))

    return rc_file


def run(cmd, capture=False, check=True, silent=False, **call_args):
    if not call_args.get("shell", False):
        cmd = shlex.split(cmd)
    # note: the cases below must be expanded to prevent pylint from becoming
    # confused about the return type (string when capture, int otherwise)
    if capture:
        return subprocess.check_output(cmd, **call_args).decode("utf8").strip()
    stderr = DEVNULL if silent else None
    call = subprocess.check_call if check else subprocess.call
    return call(cmd, stdout=DEVNULL, stderr=stderr, **call_args)


def which(exe):
    return run("which %s" % exe, check=False) == 0


def installed_lib(lib):
    """Returns true, if specific library is installed."""
    return run("ld -l:%s" % lib, check=False, silent=True) == 0


# fmt: off
# pylint: disable=missing-function-docstring,multiple-statements
RED, GREEN, YELLOW, BLUE, PINK, PLAIN = list("\033[%sm" % i for i in range(91, 96)) + ["\033[0m"]
def print_error(msg): print(RED + msg + PLAIN)
def print_success(msg): print(GREEN + msg + PLAIN)
def print_important(msg): print(YELLOW + msg + PLAIN)
def print_info(msg): print(BLUE + msg + PLAIN)
def print_header(msg): print(PINK + msg + PLAIN)
# pylint: enable=missing-function-docstring,multiple-statements
# fmt: on


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as err:
        print(RED + "error:" + PLAIN + " " + str(err))
        sys.exit(1)
    finally:
        DEVNULL.close()
