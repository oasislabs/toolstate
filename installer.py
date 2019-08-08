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
RUST_VER = 'nightly-2019-08-01'
REQUIRED_UTILS = ['curl', 'git', 'rsync']

PLAT_DARWIN = 'darwin'
PLAT_LINUX = 'linux'
RUST_SYSROOT_PREFIX = '.rustup/toolchains/%s-x86_64-' % RUST_VER
INSTALLED_DEPS_FILE = 'installed_dependencies'
DEVNULL = open('/dev/null', 'w')


def main():
    plat = platform.system().lower()
    if plat not in {PLAT_DARWIN, PLAT_LINUX} or platform.machine() != 'x86_64':
        raise RuntimeError('Unsupported platform: %s (%s)' % (plat, platform.machine()))

    missing_utils = [u for u in REQUIRED_UTILS if not which(u)]
    if missing_utils:
        raise RuntimeError('missing CLI utilities: %s' % ', '.join(missing_utils))

    args = _parse_args()

    install(plat, args)

    if args.no_modify_path:
        required_exports = get_required_exports(plat, args)
        print_important('Remember to\n')
        print_important('\n'.join('\t' + e for e in required_exports) + '\n')
    elif not which('oasis'):
        modify_path(plat, args)
        print_info('`oasis` will be available when you next log in.')
    print('')

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
        '--no-modify-path',
        action='store_true',
        help="Don't add Rust and Oasis executables to your PATH")
    parser.add_argument(
        '--force', action='store_true', help="Force install of not-deselected components.")
    parser.add_argument(
        '--no-node', action='store_true', help="Don't install Node. Even if it's missing.")
    parser.add_argument(
        '--no-rust', action='store_true', help="Don't install Rust. Even if it's missing.")
    parser.add_argument(
        '--speedrun', action='store_true', help="Accept default options for all installed tools.")
    args = parser.parse_args()

    def _ensure_dir(path):
        if osp.exists(path) and not osp.isdir(path):
            raise RuntimeError('`%s` is expected to be a directory.' % path)
        if not osp.exists(path):
            os.makedirs(path)
        return path

    args.bin_dir = _ensure_dir(osp.join(args.prefix, 'bin'))

    config_basedir = os.environ.get('XDG_CONFIG_DIR', osp.join(os.environ['HOME'], '.config'))
    args.config_dir = _ensure_dir(osp.join(config_basedir, 'oasis'))

    return args


def install(plat, args):
    installed_deps_path = osp.join(args.config_dir, INSTALLED_DEPS_FILE)
    installed_deps = set()
    if osp.isfile(installed_deps_path):
        with open(installed_deps_path) as f_installed:
            installed_deps.update(f_installed.read().rstrip().split('\n'))

    def record_install(dep):
        """Record installed tool so that they can be uninstalled later."""
        if dep in installed_deps:
            return
        with open(installed_deps_path, 'a') as f_installed:
            f_installed.write(dep + '\n')

    has_rust_install = osp.isdir(osp.expanduser('~/.cargo')) and which('rustup')
    if not args.no_rust and (args.force or not has_rust_install):
        print_header('Installing Rust')
        install_rust(args)
        record_install('rust')
        print('')

    def bin_dir(*x):
        return osp.join(args.bin_dir, *x)

    has_npm_install = which('npm') or osp.isfile(bin_dir('npm'))
    # ^ `npm` because `node` is `nodejs` on Ubuntu. `npm` is consistent.
    # Disjunction b/c `npm` might be in ~/.local/bin but not on PATH.
    if not args.no_node and (args.force or not has_npm_install):
        print_header('Installing Node')
        node_ver = install_node(plat, args)
        record_install('node-%s' % node_ver)
        print('')

    has_oasis_install = osp.isfile(bin_dir('oasis-chain')) and is_oasis(bin_dir('oasis'))
    if args.force or not has_oasis_install:
        print_header('Installing the Oasis toolchain')
        install_oasis(plat, args)
        print('')
    else:
        print_header('The Oasis toolchain is already installed.')
        print('Run `oasis set-toolchain latest` to update.\n')


def install_rust(args):
    rustup_args = '-y --default-toolchain ' + RUST_VER
    if args.no_modify_path:
        rustup_args += ' --no-modify-path'
    run("curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- %s" % rustup_args,
        shell=True)  # curl invocation taken from https://rustup.rs


def install_node(plat, args):
    node_vers = run('curl -sSL https://nodejs.org/dist/latest/', capture=True)
    node_ver, node_major_ver = re.search(r'node-(v(\d+)\.\d+\.\d+)\.tar.gz', node_vers).groups()

    if plat == PLAT_DARWIN:
        if which('brew'):  # This will a.s. be Homebrew.
            if args.force:
                run('brew uninstall node', check=False, stdout=DEVNULL, stderr=DEVNULL)
            return run('brew install %s node' % ('--force' if args.force else ''))

        if which('port'):  # There are no common non-MacPorts tools with this name.
            return run('port install node%s' % node_major_ver)

    node_dist_url = NODE_DIST_URL.format(plat=plat, ver=node_ver)

    node_tmpdir = '/tmp/node-%s' % node_ver
    if not osp.isdir(node_tmpdir):
        os.mkdir(node_tmpdir)
    run('curl -L "{url}" | tar xz -C {dir} --strip-components=1 --exclude *.md --exclude LICENSE'.
        format(url=node_dist_url, dir=node_tmpdir),
        shell=True)
    run('rsync -au %s/ %s/' % (node_tmpdir, args.prefix))
    run('rm -r %s' % node_tmpdir)
    # ^ node_tmpdir is `/tmp/node-{node_ver}`. `node_ver` is extracted from the regex above
    # and is thus constrained to be 'v' followed by a semver.

    return node_ver


def install_oasis(plat, args):
    current_tools = run('curl -sSL %s/successful_builds' % TOOLS_URL, capture=True)
    for tools in current_tools.split('\n')[::-1]:
        _date, build_plat, tool_hashes = tools.split(' ', 2)
        if build_plat == plat:
            oasis_cli_key = next(
                tool_hash for tool_hash in tool_hashes.split(' ')[2:]
                if re.match('oasis-[a-z0-f]{7,}$', tool_hash))
            break

    oasis_path = osp.join(args.prefix, 'bin', 'oasis')
    if not args.force and osp.exists(oasis_path):
        raise RuntimeError('`%s` already exists!' % oasis_path)

    s3_url = '%s/%s/current/%s' % (TOOLS_URL, plat, oasis_cli_key)
    run('curl -Lo {path} {url}'.format(path=oasis_path, url=s3_url))
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


def get_required_exports(plat, args):
    """Returns the env exports required to run the Oasis toolchain."""
    path_export = 'export PATH=%s/bin:${CARGO_HOME:-~/.cargo}/bin:$PATH' % args.prefix
    exports = [path_export]
    ld_path_key = '%s_LIBRARY_PATH' % ('DYLD' if plat == PLAT_DARWIN else 'LD')
    if RUST_SYSROOT_PREFIX not in os.environ.get(ld_path_key, ''):
        exports.append('export {0}=$(rustc --print sysroot)/lib:{0}'.format(ld_path_key))
    return exports


def modify_path(plat, args):
    """Adds the Oasis tools to the user's PATH via a profile file.
       Assumes that the current shell is the user's preferred shell
       so to not pollute other shells' profiles."""
    shell = run('ps -p $$ -oargs=', capture=True, shell=True)
    if 'zsh' in shell:
        rcfile = osp.join(os.environ.get('ZDOTDIR', '~'), '.zprofile')
    elif 'bash' in shell:
        rcfile = '~/.bash_profile'
    else:
        rcfile = '~/.profile'

    with open(osp.expanduser(rcfile), 'a') as f_rc:
        f_rc.write('\n%s\n' % '\n'.join(get_required_exports(plat, args)))


def run(cmd, capture=False, check=True, **call_args):
    if not call_args.get('shell', False):
        cmd = shlex.split(cmd)
    # note: the cases below must be expanded to prevent pylint from becoming
    # confused about the return type (string when capture, int otherwise)
    if capture:
        return subprocess.check_output(cmd, **call_args).decode('utf8').strip()
    if check:
        return subprocess.check_call(cmd, **call_args)
    return subprocess.call(cmd, **call_args)


def which(exe):
    return run('which %s' % exe, check=False, stdout=DEVNULL) == 0


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
