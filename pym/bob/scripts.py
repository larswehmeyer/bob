# Bob build tool
# Copyright (C) 2016  TechniSat Digital GmbH
#
# SPDX-License-Identifier: GPL-3.0-or-later

from . import BOB_VERSION, _enableDebug, DEBUG
from .errors import BobError
from .state import finalize
from .tty import colorize, Unbuffered, setColorMode, cleanup
from .utils import asHexStr, hashPath
import argparse
import logging
import sys
import traceback
import os

def __archive(*args, **kwargs):
     from .cmds.archive import doArchive
     doArchive(*args, **kwargs)

def __build(*args, **kwargs):
     from .cmds.build.build import doBuild
     doBuild(*args, **kwargs)

def __develop(*args, **kwargs):
     from .cmds.build.build import doDevelop
     doDevelop(*args, **kwargs)

def __clean(*args, **kwargs):
     from .cmds.build.clean import doClean
     doClean(*args, **kwargs)

def __graph(*args, **kwargs):
    from .cmds.graph import doGraph
    doGraph(*args, **kwargs)

def __help(*args, **kwargs):
    from .cmds.help import doHelp
    doHelp(availableCommands.keys(), *args, **kwargs)

def __jenkins(*args, **kwargs):
     from .cmds.jenkins import doJenkins
     doJenkins(*args, **kwargs)

def __ls(*args, **kwargs):
     from .cmds.misc import doLS
     doLS(*args, **kwargs)

def __project(*args, **kwargs):
     from .cmds.build.project import doProject
     doProject(*args, **kwargs)

def __status(*args, **kwars):
     from .cmds.build.status import doStatus
     doStatus(*args, **kwars)

def __queryscm(*args, **kwargs):
     from .cmds.misc import doQuerySCM
     doQuerySCM(*args, **kwargs)

def __querymeta(*args, **kwargs):
     from .cmds.misc import doQueryMeta
     doQueryMeta(*args, **kwargs)

def __queryrecipe(*args, **kwargs):
     from .cmds.misc import doQueryRecipe
     doQueryRecipe(*args, **kwargs)

def __querypath(*args, **kwargs):
     from .cmds.build.query import doQueryPath
     doQueryPath(*args, **kwargs)

def __download(*args, **kwargs):
    from .archive import doDownload
    doDownload(*args, **kwargs)

def __upload(*args, **kwargs):
    from .archive import doUpload
    doUpload(*args, **kwargs)

availableCommands = {
    "archive"       : ('hl', __archive, "Manage binary artifact archives"),
    "build"         : ('hl', __build, "Build (sub-)packages in release mode"),
    "dev"           : ('hl', __develop, "Build (sub-)packages in development mode"),
    "clean"         : ('hl', __clean, "Delete unused src/build/dist paths of release builds"),
    "graph"         : ('hl', __graph, "Make a interactive dependency graph"),
    "help"          : ('hl', __help, "Display help information about command"),
    "jenkins"       : ('hl', __jenkins, "Configure Jenkins server"),
    "ls"            : ('hl', __ls, "List package hierarchy"),
    "project"       : ('hl', __project, "Create project files"),
    "status"        : ('hl', __status, "Show SCM status"),

    "query-scm"     : ('ll', __queryscm, "Query SCM information"),
    "query-recipe"  : ('ll', __queryrecipe, "Query package sources"),
    "query-path"    : ('ll', __querypath, "Query path information"),
    "query-meta"    : ('ll', __querymeta, "Query Package meta information"),

    "_download"     : (None, __download, ""),
    "_upload"       : (None, __upload, ""),
}

def describeCommands():
    hlCmds = "\n".join(sorted([ "  {:16s}{}".format(k, v[2])
        for (k,v) in availableCommands.items() if v[0] == 'hl' ]))
    llCmds = "\n".join(sorted([ "  {:16s}{}".format(k, v[2])
        for (k,v) in availableCommands.items() if v[0] == 'll' ]))
    return """
The following high level commands are available:

{}

The following scripting commands are available:

{}

See 'bob <command> -h' for more information on a specific command.""".format(hlCmds, llCmds);

def catchErrors(fun, *args, **kwargs):
    try:
        ret = fun(*args, **kwargs)
    except BrokenPipeError:
        # explicitly close stderr to suppress further error messages
        sys.stderr.close()
        ret = 0
    except BobError as e:
        print(e, file=sys.stderr)
        ret = 1
    except KeyboardInterrupt:
        ret = 2
    except ImportError as e:
        if e.name:
            print(colorize("Python module '{}' seems to be missing. ".format(e.name) +
                           "Please check your installation...", "31;1"),
                  file=sys.stderr)
        else:
            print(colorize(str(e) + " Please check your installation...", "31;1"),
                  file=sys.stderr)
        ret = 3
    except Exception:
        print(colorize("""An internal Exception has occured. This should not have happenend.
Please open an issue at https://github.com/BobBuildTool/bob with the following backtrace:""", "31;1"), file=sys.stderr)
        print("Bob version", BOB_VERSION, file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        ret = 3

    return ret

def bob(bobRoot = None):
    if not bobRoot:
        bobRoot = os.path.dirname(os.path.realpath(os.path.abspath(sys.argv[0])))
        bobRoot = os.path.join(bobRoot, "..")
    origSysStdOut = sys.stdout
    origSysStdErr = sys.stderr
    logging.disable(logging.ERROR)

    # Prevent any buffering. Even on a tty Python is doing line buffering.
    sys.stdout = Unbuffered(sys.stdout)
    sys.stderr = Unbuffered(sys.stderr)

    def cmd():
        parser = argparse.ArgumentParser(prog="bob",
                                         description="Bob build tool\n" + describeCommands(),
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('-C', '--directory', dest='directory', action='append', help="Change to DIRECTORY before doing anything", metavar="DIRECTORY")
        parser.add_argument('--version', dest='version', action='store_true', help="Show version")
        parser.add_argument('--debug',   dest='debug', help="Enable debug modes (shl,pkgck,ngd)")
        parser.add_argument('-i', dest='ignore_commandCfg', default=False, action='store_true',
                help="Use bob's default argument settings and do not use commands section of the userconfig.")
        parser.add_argument('--color', dest='color_mode',
                help="Color mode of console output (default: auto)",
                choices=['never', 'always', 'auto'])
        parser.add_argument('command', nargs='?', help="Command to execute")
        parser.add_argument('args', nargs=argparse.REMAINDER, help="Arguments to command")

        args = parser.parse_args(sys.argv[1:])
        if args.version:
            print("Bob version", BOB_VERSION)
            return 0

        if args.debug:
            _enableDebug(args.debug)

        if args.ignore_commandCfg:
            from .input import RecipeSet
            RecipeSet.ignoreCommandCfg()

        if args.color_mode:
            from .input import RecipeSet
            RecipeSet.setColorModeCfg(args.color_mode)

        if args.command is None:
            print("No command specified. Use 'bob -h' for help.", file=sys.stderr)
            return 2

        # Shortcut for 'bob help' displaying the same help screen like 'bob
        # --help' would do. The 'bob help COMMAND' case is handled by help from
        # the available commands.
        if args.command == "help" and len(args.args) == 0:
            parser.print_help()
            return 0

        if args.command in availableCommands:
            if args.directory is not None:
                for i in args.directory:
                    try:
                        os.chdir(i)
                    except OSError as e:
                        print("bob -C: unable to change directory:", str(e), file=sys.stderr)
                        return 1
            cmd = availableCommands[args.command][1]
            if DEBUG['prof']:
                import cProfile, pstats
                pr = cProfile.Profile()
                pr.enable()
                cmd(args.args, bobRoot)
                pr.disable()
                ps = pstats.Stats(pr, stream=sys.stderr).sort_stats('tottime')
                print("Bob", BOB_VERSION, "profile:", file=sys.stderr)
                print("Args:", sys.argv[1:], file=sys.stderr)
                ps.print_stats()
            else:
                cmd(args.args, bobRoot)
            return 0
        else:
            print("Don't know what to do for '{}'. Use 'bob -h' for help".format(args.command), file=sys.stderr)
            return 2

    try:
        ret = catchErrors(cmd)
    finally:
        cleanup()
        sys.stdout = origSysStdOut
        sys.stderr = origSysStdErr
        finalize()

    return ret

def hashEngine():
    parser = argparse.ArgumentParser(description="Create hash based on spec.")
    parser.add_argument('-o', dest="output", metavar="OUTPUT", default="-", help="Output file (default: stdout)")
    parser.add_argument('--state', help="State cache directory")
    parser.add_argument('spec', nargs='?', default="-", help="Spec input (default: stdin)")
    args = parser.parse_args()

    def cmd():
        try:
            if args.spec == "-":
                inFile = sys.stdin
            else:
                inFile = open(args.spec, "r")

            res = b''
            for cmd in inFile:
                res += __process(cmd.strip(), inFile, args.state)
            if args.output == "-":
                sys.stdout.buffer.write(res)
            else:
                with open(args.output, "wb") as f:
                    f.write(res)
        except OSError as e:
            raise BobError("IO error: " + str(e))

        return 0

    return catchErrors(cmd)

def auditEngine():
    parser = argparse.ArgumentParser(description="Create audit trail.")
    parser.add_argument('-o', dest="output", metavar="OUTPUT", default="-", help="Output file (default: stdout)")
    parser.add_argument("-D", dest="defines", action="append", default=[], nargs=2)
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--env")
    parser.add_argument("-E", dest="metaEnv", action="append", default=[], nargs=2)
    parser.add_argument("--recipes")
    parser.add_argument("--sandbox")
    parser.add_argument("--scm", action="append", default=[], nargs=3)
    parser.add_argument("--tool", action="append", default=[], nargs=2)
    parser.add_argument("variantID")
    parser.add_argument("buildID")
    parser.add_argument("resultHash")
    args = parser.parse_args()

    def cmd():
        from .audit import Audit
        import json
        import os
        try:
            gen = Audit.create(bytes.fromhex(args.variantID), bytes.fromhex(args.buildID),
                bytes.fromhex(args.resultHash))
        except ValueError:
            raise BuildError("Invalid digest argument")
        if args.env is not None: gen.setEnv(args.env)
        for (name, value) in args.metaEnv: gen.addMetaEnv(name, value)
        for (name, value) in args.defines: gen.addDefine(name, value)
        for (name, workspace, dir) in args.scm: gen.addScm(name, workspace, dir)
        for (name, audit) in args.tool: gen.addTool(name, audit)
        try:
            if args.recipes is not None: gen.setRecipesData(json.loads(args.recipes))
        except ValueError as e:
            raise BuildError("Invalid recipes json: " + str(e))
        if args.sandbox is not None: gen.setSandbox(args.sandbox)
        for arg in args.arg: gen.addArg(arg)

        if args.output == "-":
            gen.save(sys.stdout.buffer)
        else:
            gen.save(args.output)

        return 0

    return catchErrors(cmd)

def __process(l, inFile, stateDir):
    if l.startswith("="):
        return bytes.fromhex(l[1:])
    elif l.startswith("<"):
        with open(l[1:], "rb") as f:
            return f.read()
    elif l.startswith("{"):
        import hashlib
        fn = l[1:]
        skipEmpty = False
        if fn.startswith("?"):
            fn = fn[1:]
            skipEmpty = True
        return __processBlock(hashlib.new(fn), inFile, stateDir, skipEmpty)
    elif l.startswith("["):
        exp,sep,l = l[1:].partition("]")
        start,sep,end = exp.partition(":")
        start = int(start) if start else None
        end = int(end) if end else None
        return __process(l, inFile, stateDir)[start:end]
    elif l.startswith("#"):
        import os.path
        if stateDir:
            stateFile = os.path.join(stateDir, l[1:].replace(os.sep, "_"))
        else:
            stateFile = None
        return hashPath(l[1:], stateFile)
    elif l.startswith("g"):
        from .scm.git import GitScm
        return bytes.fromhex(GitScm.processLiveBuildIdSpec(l[1:]))
    elif l:
        print("Malformed spec:", l, file=sys.stderr)
        sys.exit(1)
    else:
        return b''

def __processBlock(h, inFile, stateDir, skipEmpty):
    for l in inFile:
        l = l.strip()
        if l.startswith("}"):
            return h.digest() if not skipEmpty else b''
        else:
            data = __process(l, inFile, stateDir)
            if data: skipEmpty = False
            h.update(data)
    print("Malformed spec: unfinished block", file=sys.stderr)
    sys.exit(1)

if __name__ == '__main__':
    if sys.argv[1] == 'bob':
        rootDir = sys.argv[2]
        del sys.argv[1:3]
        sys.exit(bob(rootDir))

