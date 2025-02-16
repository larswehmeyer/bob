# Bob build tool
# Copyright (C) 2016-2018  TechniSat Digital GmbH
#
# SPDX-License-Identifier: GPL-3.0-or-later

from ...archive import getArchiver
from ...errors import BuildError
from ...input import RecipeSet
from ...tty import setVerbosity, setTui
from ...utils import copyTree, processDefines
import argparse
import asyncio
import concurrent.futures
import datetime
import multiprocessing
import os
import signal
import subprocess
import sys
import time

from .state import DevelopDirOracle
from .builder import LocalBuilder

def dummy():
    pass

def runHook(recipes, hook, args):
    hookCmd = recipes.getBuildHook(hook)
    ret = True
    if hookCmd:
        try:
            hookCmd = os.path.expanduser(hookCmd)
            ret = subprocess.call([hookCmd] + args) == 0
        except OSError as e:
            raise BuildError(hook + ": cannot run '" + hookCmd + ": " + str(e))

    return ret

def commonBuildDevelop(parser, argv, bobRoot, develop):
    def _downloadArgument(arg):
        if arg.startswith('packages=') or arg in ['yes', 'no', 'deps', 'forced', 'forced-deps', 'forced-fallback']:
            return arg
        raise argparse.ArgumentTypeError("{} invalid.".format(arg))

    parser.add_argument('packages', metavar='PACKAGE', type=str, nargs='+',
        help="(Sub-)package to build")
    parser.add_argument('--destination', metavar="DEST", default=None,
        help="Destination of build result (will be overwritten!)")
    parser.add_argument('-j', '--jobs', default=None, type=int, nargs='?', const=...,
        help="Specifies  the  number of jobs to run simultaneously.")
    parser.add_argument('-k', '--keep-going', default=None, action='store_true',
        help="Continue  as much as possible after an error.")
    parser.add_argument('-f', '--force', default=None, action='store_true',
        help="Force execution of all build steps")
    parser.add_argument('-n', '--no-deps', default=None, action='store_true',
        help="Don't build dependencies")
    parser.add_argument('-p', '--with-provided', dest='build_provided', default=None, action='store_true',
        help="Build provided dependencies")
    parser.add_argument('--without-provided', dest='build_provided', default=None, action='store_false',
        help="Build without provided dependencies")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-b', '--build-only', dest='build_mode', default=None,
        action='store_const', const='build-only',
        help="Don't checkout, just build and package")
    group.add_argument('-B', '--checkout-only', dest='build_mode',
        action='store_const', const='checkout-only',
        help="Don't build, just check out sources")
    group.add_argument('--normal', dest='build_mode',
        action='store_const', const='normal',
        help="Checkout, build and package")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--clean', action='store_true', default=None,
        help="Do clean builds (clear build directory)")
    group.add_argument('--incremental', action='store_false', dest='clean',
        help="Reuse build directory for incremental builds")
    parser.add_argument('--always-checkout', default=[], action='append', metavar="RE",
        help="Regex pattern of packages that should always be checked out")
    parser.add_argument('--resume', default=False, action='store_true',
        help="Resume build where it was previously interrupted")
    parser.add_argument('-q', '--quiet', default=0, action='count',
        help="Decrease verbosity (may be specified multiple times)")
    parser.add_argument('-v', '--verbose', default=0, action='count',
        help="Increase verbosity (may be specified multiple times)")
    parser.add_argument('--no-logfiles', default=None, action='store_true',
        help="Disable logFile generation.")
    parser.add_argument('-D', default=[], action='append', dest="defines",
        help="Override default environment variable")
    parser.add_argument('-c', dest="configFile", default=[], action='append',
        help="Use config File")
    parser.add_argument('-e', dest="white_list", default=[], action='append', metavar="NAME",
        help="Preserve environment variable")
    parser.add_argument('-E', dest="preserve_env", default=False, action='store_true',
        help="Preserve whole environment")
    parser.add_argument('--upload', default=None, action='store_true',
        help="Upload to binary archive")
    parser.add_argument('--link-deps', default=None, help="Add linked dependencies to workspace paths",
        dest='link_deps', action='store_true')
    parser.add_argument('--no-link-deps', default=None, help="Do not add linked dependencies to workspace paths",
        dest='link_deps', action='store_false')
    parser.add_argument('--download', metavar="MODE", default=None,
        help="Download from binary archive (yes, no, deps, forced, forced-deps, forced-fallback, packages=<packages>",
        type=_downloadArgument)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--sandbox', action='store_true', default=None,
        help="Enable sandboxing")
    group.add_argument('--no-sandbox', action='store_false', dest='sandbox',
        help="Disable sandboxing")
    parser.add_argument('--clean-checkout', action='store_true', default=None, dest='clean_checkout',
        help="Do a clean checkout if SCM state is dirty.")
    args = parser.parse_args(argv)

    defines = processDefines(args.defines)

    startTime = time.time()

    try:
        from ...develop.make import makeSandboxHelper
        makeSandboxHelper()
    except ImportError:
        pass

    if sys.platform == 'win32':
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
        multiprocessing.set_start_method('spawn')
        executor = concurrent.futures.ProcessPoolExecutor()
    else:
        # The ProcessPoolExecutor is a barely usable for our interactive use
        # case. On SIGINT any busy executor should stop. The only way how this
        # does not explode is that we ignore SIGINT before spawning the process
        # pool and re-enable SIGINT in every executor. In the main process we
        # have to ignore BrokenProcessPool errors as we will likely hit them.
        # To "prime" the process pool a dummy workload must be executed because
        # the processes are spawned lazily.
        loop = asyncio.get_event_loop()
        origSigInt = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        # fork early before process gets big
        if sys.platform == 'msys':
            multiprocessing.set_start_method('fork')
        else:
            multiprocessing.set_start_method('forkserver')
        executor = concurrent.futures.ProcessPoolExecutor()
        executor.submit(dummy).result()
        signal.signal(signal.SIGINT, origSigInt)
    loop.set_default_executor(executor)

    try:
        recipes = RecipeSet()
        recipes.defineHook('releaseNameFormatter', LocalBuilder.releaseNameFormatter)
        recipes.defineHook('developNameFormatter', LocalBuilder.developNameFormatter)
        recipes.defineHook('developNamePersister', None)
        recipes.setConfigFiles(args.configFile)
        recipes.parse()

        # if arguments are not passed on cmdline use them from default.yaml or set to default yalue
        if develop:
            cfg = recipes.getCommandConfig().get('dev', {})
        else:
            cfg = recipes.getCommandConfig().get('build', {})

        defaults = {
                'destination' : '',
                'force' : False,
                'no_deps' : False,
                'build_mode' : 'normal',
                'clean' : not develop,
                'upload' : False,
                'download' : "deps" if develop else "yes",
                'sandbox' : not develop,
                'clean_checkout' : False,
                'no_logfiles' : False,
                'link_deps' : True,
                'jobs' : 1,
                'keep_going' : False,
            }

        for a in vars(args):
            if getattr(args, a) == None:
                setattr(args, a, cfg.get(a, defaults.get(a)))

        if args.jobs is ...:
            args.jobs = os.cpu_count()
        elif args.jobs <= 0:
            parser.error("--jobs argument must be greater than zero!")

        envWhiteList = recipes.envWhiteList()
        envWhiteList |= set(args.white_list)

        if develop:
            nameFormatter = recipes.getHook('developNameFormatter')
            developPersister = DevelopDirOracle(nameFormatter, recipes.getHook('developNamePersister'))
            nameFormatter = developPersister.getFormatter()
        else:
            nameFormatter = recipes.getHook('releaseNameFormatter')
            nameFormatter = LocalBuilder.releaseNamePersister(nameFormatter)
        nameFormatter = LocalBuilder.makeRunnable(nameFormatter)
        packages = recipes.generatePackages(nameFormatter, defines, args.sandbox)
        if develop: developPersister.prime(packages)

        verbosity = cfg.get('verbosity', 0) + args.verbose - args.quiet
        setVerbosity(verbosity)
        builder = LocalBuilder(recipes, verbosity, args.force,
                               args.no_deps, True if args.build_mode == 'build-only' else False,
                               args.preserve_env, envWhiteList, bobRoot, args.clean,
                               args.no_logfiles)

        builder.setArchiveHandler(getArchiver(recipes))
        builder.setUploadMode(args.upload)
        builder.setDownloadMode(args.download)
        builder.setCleanCheckout(args.clean_checkout)
        builder.setAlwaysCheckout(args.always_checkout + cfg.get('always_checkout', []))
        builder.setLinkDependencies(args.link_deps)
        builder.setJobs(args.jobs)
        builder.setKeepGoing(args.keep_going)
        if args.resume: builder.loadBuildState()

        backlog = []
        providedBacklog = []
        results = []
        for p in args.packages:
            for package in packages.queryPackagePath(p):
                packageStep = package.getPackageStep()
                backlog.append(packageStep)
                # automatically include provided deps when exporting
                build_provided = (args.destination and args.build_provided == None) or args.build_provided
                if build_provided: providedBacklog.extend(packageStep._getProvidedDeps())

        success = runHook(recipes, 'preBuildHook',
            ["/".join(p.getPackage().getStack()) for p in backlog])
        if not success:
            raise BuildError("preBuildHook failed!",
                help="A preBuildHook is set but it returned with a non-zero status.")
        success = False
        if args.jobs > 1:
            setTui(args.jobs)
            builder.enableBufferedIO()
        try:
            builder.cook(backlog, True if args.build_mode == 'checkout-only' else False)
            for p in backlog:
                resultPath = p.getWorkspacePath()
                if resultPath not in results:
                    results.append(resultPath)
            builder.cook(providedBacklog, True if args.build_mode == 'checkout-only' else False, 1)
            for p in providedBacklog:
                resultPath = p.getWorkspacePath()
                if resultPath not in results:
                    results.append(resultPath)
            success = True
        finally:
            if args.jobs > 1: setTui(1)
            builder.saveBuildState()
            runHook(recipes, 'postBuildHook', ["success" if success else "fail"] + results)

    finally:
        executor.shutdown()
        loop.close()

    # tell the user
    if results:
        if len(results) == 1:
            print("Build result is in", results[0])
        else:
            print("Build results are in:\n  ", "\n   ".join(results))

        endTime = time.time()
        stats = builder.getStatistic()
        activeOverrides = len(stats.getActiveOverrides())
        print("Duration: " + str(datetime.timedelta(seconds=(endTime - startTime))) + ", "
                + str(stats.checkouts)
                    + " checkout" + ("s" if (stats.checkouts != 1) else "")
                    + " (" + str(activeOverrides) + (" overrides" if (activeOverrides != 1) else " override") + " active), "
                + str(stats.packagesBuilt)
                    + " package" + ("s" if (stats.packagesBuilt != 1) else "") + " built, "
                + str(stats.packagesDownloaded) + " downloaded.")

        # copy build result if requested
        ok = True
        if args.destination:
            for result in results:
                ok = copyTree(result, args.destination) and ok
        if not ok:
            raise BuildError("Could not copy everything to destination. Your aggregated result is probably incomplete.")
    else:
        print("Your query matched no packages. Naptime!")

def doBuild(argv, bobRoot):
    parser = argparse.ArgumentParser(prog="bob build", description='Build packages in release mode.')
    commonBuildDevelop(parser, argv, bobRoot, False)

def doDevelop(argv, bobRoot):
    parser = argparse.ArgumentParser(prog="bob dev", description='Build packages in development mode.')
    commonBuildDevelop(parser, argv, bobRoot, True)

