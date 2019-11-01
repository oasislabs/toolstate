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

TOOLS_URL = 'https://tools.oasis.dev'
NODE_DIST_URL = 'https://nodejs.org/dist/{ver}/node-{ver}-{plat}-x64.tar.gz'
RUST_VER = 'nightly-2019-08-26'
REQUIRED_UTILS = ['cc', 'curl', 'git']

PLAT_DARWIN = 'darwin'
PLAT_LINUX = 'linux'
RUST_SYSROOT_PREFIX = 'toolchains/%s-x86_64-' % RUST_VER
INSTALLED_DEPS_FILE = 'installed_dependencies'
DEVNULL = open('/dev/null', 'w')


def main():
    missing_utils = [u for u in REQUIRED_UTILS if not which(u)]
    if missing_utils:
        raise RuntimeError('missing system utilities: %s' % ', '.join(missing_utils))

    env_info = _get_env_info()

    if env_info.plat not in {PLAT_DARWIN, PLAT_LINUX} or platform.machine() != 'x86_64':
        raise RuntimeError('Unsupported platform: %s (%s)' % (env_info.plat, platform.machine()))

    args = _parse_args()

    install(args, env_info)

    has_oasis_on_path = which('oasis')
    if args.no_modify_shell and not has_oasis_on_path:
        required_exports = get_shell_additions(args, env_info)
        print_important('Remember to run the following before using `oasis`:\n')
        print_important('\n'.join('    ' + e for e in required_exports))
        print('')
    elif not has_oasis_on_path:
        rc_file = modify_shell_profile(args, env_info)
        print_info('`oasis` will be available when you next log in.\n')
        print_info('To configure your current shell run `source %s`\n' % rc_file)

    print_success("You're ready to start developing on Oasis!")


def _parse_args():
    if 'XDG_DATA_DIR' in os.environ:
        default_prefix = osp.dirname(os.environ['XDG_DATA_DIR'])
    else:
        default_prefix = osp.join(os.environ['HOME'], '.local')

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--toolchain',
        default='latest',
        help="Which version of the Oasis toolchain to install. Default: latest")
    parser.add_argument(
        '--prefix',
        default=default_prefix,
        help="Installation prefix. Default: `%s`" % default_prefix)
    parser.add_argument(
        '--no-modify-shell', action='store_true', help="Don't modify your shell profile")
    parser.add_argument(
        '--force', action='store_true', help="Force install of not-deselected components.")
    parser.add_argument(
        '--no-node', action='store_true', help="Don't install Node. Even if it's missing.")
    parser.add_argument(
        '--no-rust', action='store_true', help="Don't install Rust. Even if it's missing.")
    parser.add_argument(
        '--speedrun', action='store_true', help="Accept default options for all installed tools.")
    args = parser.parse_args()

    args.bin_dir = _ensure_dir(osp.join(args.prefix, 'bin'))

    return args


def _get_env_info():
    home_dir = os.environ['HOME']
    data_home = os.environ.get('XDG_DATA_HOME', osp.join(home_dir, '.local', 'share'))

    return argparse.Namespace(
        home_dir=home_dir,
        data_dir=_ensure_dir(osp.join(data_home, 'oasis')),
        rustup_home=os.environ.get('RUSTUP_HOME', osp.join(home_dir, '.rustup')),
        cargo_home=os.environ.get('CARGO_HOME', osp.join(home_dir, '.cargo')),
        shell=os.environ.get('SHELL', 'sh'),
        plat=platform.system().lower(),
    )


def _ensure_dir(path):
    if osp.exists(path) and not osp.isdir(path):
        raise RuntimeError('`%s` is expected to be a directory.' % path)
    if not osp.exists(path):
        os.makedirs(path)
    return path


def install(args, env_info):
    installed_deps_path = osp.join(env_info.data_dir, INSTALLED_DEPS_FILE)
    preinstalled_deps = set()
    if osp.isfile(installed_deps_path):
        with open(installed_deps_path) as f_installed:
            preinstalled_deps.update(f_installed.read().rstrip().split('\n'))
    else:
        with open(installed_deps_path, 'w') as _:
            pass

    def _record_install(dep):
        """Record installed tool so that they can be uninstalled later."""
        if dep in preinstalled_deps:
            return
        with open(installed_deps_path, 'a') as f_installed:
            f_installed.write(dep + '\n')

    has_rust_install = which('rustup')
    if not args.no_rust and (args.force or not has_rust_install):
        print_header('Installing Rust')
        install_rust()
        _record_install('rust')
        print('')
    if not args.no_rust:
        rustup_bin = osp.join(env_info.cargo_home, 'bin', 'rustup')
        run('%s toolchain install %s' % (rustup_bin, RUST_VER), silent=True)
        run('%s target add wasm32-wasi --toolchain %s' % (rustup_bin, RUST_VER), silent=True)

    def bin_dir(*x):
        return osp.join(args.bin_dir, *x)

    has_npm_install = which('npm') or osp.isfile(bin_dir('npm'))
    # ^ `npm` because `node` is `nodejs` on Ubuntu. `npm` is consistent.
    # Disjunction b/c `npm` might be in ~/.local/bin but not on PATH.
    if not args.no_node and (args.force or not has_npm_install):
        print_header('Installing Node')
        node_ver = install_node(args, env_info)
        _record_install('node-%s' % node_ver)
        print('')

    has_oasis_install = osp.isfile(bin_dir('oasis-chain')) and is_oasis(bin_dir('oasis'))
    if args.force or not has_oasis_install:
        print_header('Installing the Oasis toolchain')
        install_oasis(args, env_info)
        print('')
    else:
        print_header('The Oasis toolchain is already installed.')
        print('Run `oasis set-toolchain latest` to update.\n')


def install_rust():
    rustup_args = '-y --no-modify-path --default-toolchain ' + RUST_VER
    run("curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- %s" % rustup_args,
        shell=True)  # curl invocation taken from https://rustup.rs


def install_node(args, env_info):
    node_vers = run('curl -sSL https://nodejs.org/dist/latest-v12.x/', capture=True)
    node_ver, node_major_ver = re.search(r'node-(v(\d+)\.\d+\.\d+)\.tar.gz', node_vers).groups()

    if env_info.plat == PLAT_DARWIN:
        if which('brew'):  # This will a.s. be Homebrew.
            if args.force:
                run('brew uninstall node', check=False, silent=True)
            return run('brew install %s node' % ('--force' if args.force else ''))

        if which('port'):  # There are no common non-MacPorts tools with this name.
            return run('port install node%s' % node_major_ver)

    curl = 'curl -L# "%s"' % NODE_DIST_URL.format(plat=env_info.plat, ver=node_ver)
    tar = 'tar xz -C %s --strip-components=1 --exclude "*.md" --exclude LICENSE' % args.prefix
    run('{curl} | {tar}'.format(curl=curl, tar=tar), shell=True)

    return node_ver


def install_oasis(args, env_info):
    current_tools = run('curl -sSL %s/successful_builds' % TOOLS_URL, capture=True)
    for tools in current_tools.split('\n')[::-1]:
        _date, build_plat, tool_hashes = tools.split(' ', 2)
        if build_plat == env_info.plat:
            oasis_cli_key = next(
                tool_hash for tool_hash in tool_hashes.split(' ')[2:]
                if re.match('oasis-[a-z0-f]{7,}$', tool_hash))
            break

    oasis_path = osp.join(args.prefix, 'bin', 'oasis')
    if not args.force and osp.exists(oasis_path):
        raise RuntimeError('`%s` already exists!' % oasis_path)

    s3_url = '%s/%s/current/%s' % (TOOLS_URL, env_info.plat, oasis_cli_key)
    run('curl -sSLo {path} {url}'.format(path=oasis_path, url=s3_url))
    run('chmod a+x %s' % oasis_path)
    if args.speedrun:
        oasis_cp = subprocess.Popen(
            oasis_path, stdin=subprocess.PIPE, stdout=DEVNULL, stderr=DEVNULL)
        oasis_cp.stdin.close()  # EOF will cause config prompt to use default options
        if oasis_cp.wait() != 0:
            print_error('Unable to set default configuration for Oasis CLI (--speedrun)')

    run('%s set-toolchain %s' % (oasis_path, args.toolchain), env=_skipconfig_env())


def _skipconfig_env():
    env = dict(os.environ)
    env['OASIS_SKIP_GENERATE_CONFIG'] = '1'
    return env


def is_oasis(path):
    """Returns whether the binary at `path` is the Oasis CLI."""
    if not osp.isfile(path) or osp.isdir(path):
        return False
    try:
        help_msg = run('%s --help' % path, capture=True, env=_skipconfig_env())
        return 'Oasis developer tools' in help_msg
    except subprocess.CalledProcessError:
        pass
    return False


def get_shell_additions(args, env_info):
    """Returns the env exports required to run the Oasis toolchain."""
    path_export = 'export PATH=%s/bin:${CARGO_HOME:-~/.cargo}/bin:$PATH' % args.prefix
    exports = [path_export]
    ld_path_key = '%s_LIBRARY_PATH' % ('DYLD' if env_info.plat == PLAT_DARWIN else 'LD')
    if osp.join(env_info.rustup_home, RUST_SYSROOT_PREFIX) not in os.environ.get(ld_path_key, ''):
        exports.append('export {0}=$(rustc --print sysroot)/lib:${0}'.format(ld_path_key))

    data_dir = osp.join(args.prefix, 'share', 'oasis')
    if 'zsh' in env_info.shell:
        exports.append('fpath=("%s" $fpath)' % data_dir)
    elif 'bash' in env_info.shell:
        exports.append('source "%s"' % osp.join(data_dir, 'completions.sh'))

    return exports


def modify_shell_profile(args, env_info):
    """Adds the Oasis tools to the user's PATH via a profile file.
       Assumes that the current shell is the user's preferred shell
       so to not pollute other shells' profiles."""
    if 'zsh' in env_info.shell:
        rcfile = osp.join(os.environ.get('ZDOTDIR', '~'), '.zprofile')
    elif 'bash' in env_info.shell:
        if env_info.plat == PLAT_DARWIN:
            rcfile = '~/.bash_profile'
        else:
            rcfile = '~/.bashrc'
    else:
        rcfile = '~/.profile'
    rc_file = osp.expanduser(rcfile)

    required_exports = get_shell_additions(args, env_info)

    rc_lines = set()
    if osp.isfile(rc_file):
        with open(rc_file) as f_rc:
            rc_lines = set(line.rstrip() for line in f_rc)

    if not all(export in rc_lines for export in required_exports):
        with open(rc_file, 'a') as f_rc:
            f_rc.write('\n%s\n' % '\n'.join(required_exports))

    return rc_file


def run(cmd, capture=False, check=True, silent=False, **call_args):
    if not call_args.get('shell', False):
        cmd = shlex.split(cmd)
    # note: the cases below must be expanded to prevent pylint from becoming
    # confused about the return type (string when capture, int otherwise)
    if capture:
        return subprocess.check_output(cmd, **call_args).decode('utf8').strip()
    stderr = DEVNULL if silent else None
    call = subprocess.check_call if check else subprocess.call
    return call(cmd, stdout=DEVNULL, stderr=stderr, **call_args)


def which(exe):
    return run('which %s' % exe, check=False) == 0


# yapf:disable pylint:disable=invalid-name,multiple-statements,missing-docstring
RED, GREEN, YELLOW, BLUE, PINK, PLAIN = list('\033[%sm' % i for i in range(91, 96)) + ['\033[0m']
def print_error(s): print(RED + s + PLAIN)
def print_success(s): print(GREEN + s + PLAIN)
def print_important(s): print(YELLOW + s + PLAIN)
def print_info(s): print(BLUE + s + PLAIN)
def print_header(s): print(PINK + s + PLAIN)
# yapf:enable pylint:enable=invalid-name,multiple-statements,missing-docstring

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as err:
        print(RED + 'error:' + PLAIN + ' ' + str(err))
    finally:
        DEVNULL.close()
