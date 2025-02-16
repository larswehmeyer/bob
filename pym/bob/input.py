# Bob build tool
# Copyright (C) 2016  TechniSat Digital GmbH
#
# SPDX-License-Identifier: GPL-3.0-or-later

from . import BOB_VERSION, BOB_INPUT_HASH, DEBUG
from .errors import ParseError
from .fingerprints import mangleFingerprints
from .pathspec import PackageSet
from .scm import CvsScm, GitScm, SvnScm, UrlScm, ScmOverride, auditFromDir, getScm
from .state import BobState
from .stringparser import checkGlobList, Env, DEFAULT_STRING_FUNS
from .tty import InfoOnce, Warn, WarnOnce, setColorMode
from .utils import asHexStr, joinScripts, sliceString, compareVersion, binStat, updateDicRecursive, hashString
from abc import ABCMeta, abstractmethod
from base64 import b64encode
from itertools import chain
from glob import glob
from pipes import quote
from os.path import expanduser
from string import Template
from textwrap import dedent
import copy
import hashlib
import fnmatch
import os, os.path
import pickle
import re
import schema
import sqlite3
import struct
import sys
import yaml

warnFilter = WarnOnce("The filter keyword is experimental and might change or vanish in the future.")
warnDepends = WarnOnce("The same package is named multiple times as dependency!",
    help="Only the first such incident is reported. This behavior will be treated as an error in the future.")
warnDeprecatedPluginState = Warn("Plugin uses deprecated 'bob.input.PluginState' API!")
warnDeprecatedStringFn = Warn("Plugin uses deprecated 'stringFunctions' API!")

def overlappingPaths(p1, p2):
    p1 = os.path.normcase(os.path.normpath(p1)).split(os.sep)
    if p1 == ["."]: p1 = []
    p2 = os.path.normcase(os.path.normpath(p2)).split(os.sep)
    if p2 == ["."]: p2 = []
    for i in range(min(len(p1), len(p2))):
        if p1[i] != p2[i]: return False
    return True

def __maybeGlob(pred):
    if pred.startswith("!"):
        pred = pred[1:]
        if any(i in pred for i in '*?[]'):
            return lambda prev, elem: False if fnmatch.fnmatchcase(elem, pred) else prev
        else:
            return lambda prev, elem: False if elem == pred else prev
    else:
        if any(i in pred for i in '*?[]'):
            return lambda prev, elem: True if fnmatch.fnmatchcase(elem, pred) else prev
        else:
            return lambda prev, elem: True if elem == pred else prev

def maybeGlob(pattern):
    if isinstance(pattern, list):
        return [ __maybeGlob(p) for p in pattern ]
    else:
        return None

class __uidGen:
    def __init__(self):
        self.cur = 0
    def get(self):
        self.cur += 1
        return self.cur

uidGen = __uidGen().get

class DigestHasher:
    def __init__(self):
        self.__recipes = bytearray()
        self.__host = bytearray()

    def update(self, real):
        """Add bytes to recipe-internal part of digest."""
        self.__recipes.extend(real)

    def fingerprint(self, imag):
        """Add bytes of fingerprint to host part of digest."""
        self.__host.extend(imag)

    def digest(self):
        """Calculate final digest value.

        If no host fingerprints were added only the recipe-internal digest is
        emitted. Otherwise the fingerprint digest is appended. This keeps the
        calculation backwards compatible (Bob <0.15).
        """
        if self.__host:
            return hashlib.sha1(self.__recipes).digest() + \
                   hashlib.sha1(self.__host).digest()
        else:
            return hashlib.sha1(self.__recipes).digest()

    @staticmethod
    def sliceRecipes(digest):
        """Extract recipe-internal digest part."""
        return digest[:20]

    @staticmethod
    def sliceHost(digest):
        """Extract host fingerprint digest part (if any)."""
        return digest[20:]


class PluginProperty:
    """Base class for plugin property handlers.

    A plugin should sub-class this class to parse custom properties in a
    recipe. For each recipe an object of that class is created then. The
    default constructor just stores the *present* and *value* parameters as
    attributes in the object.

    :param bool present: True if property is present in recipe
    :param value: Unmodified value of property from recipe or None if not present.
    """

    def __init__(self, present, value):
        self.present = present
        self.value = value

    def inherit(self, cls):
        """Inherit from a class.

        The default implementation will use the value from the class if the
        property was not present. Otherwise the class value will be ignored.
        """
        if not self.present:
            self.present = cls.present
            self.value = cls.value

    def isPresent(self):
        """Return True if the property was present in the recipe."""
        return self.present

    def getValue(self):
        """Get (parsed) value of the property."""
        return self.value

    @staticmethod
    def validate(data):
        """Validate type of property.

        Ususally the plugin will reimplement this static method and return True
        only if *data* has the expected type. The default implementation will
        always return True.

        :param data: Parsed property data from the recipe
        :return: True if data has expected type, otherwise False.
        """
        return True


class PluginState:
    """Base class for plugin state trackers.

    State trackers are used by plugins to compute the value of one or more
    properties as the dependency tree of all recipes is traversed.

    .. attention::
        Objects of this class are tested for equivalence. The default
        implementation compares all members of the involved objects. If custom
        types are stored in the object you have to provide a suitable
        ``__eq__`` and ``__ne__`` implementation because Python falls back to
        object identity which might not be correct.  If these operators are not
        working correctly then Bob may slow down considerably.
    """

    def __eq__(self, other):
        return vars(self) == vars(other)

    def __ne__(self, other):
        return vars(self) != vars(other)

    def copy(self):
        """Return a copy of the object.

        The default implementation uses copy.deepcopy() which should usually be
        enough. If the plugin uses a sophisticated state tracker, especially
        when holding references to created packages, it might be usefull to
        provide a specialized implementation.
        """
        return copy.deepcopy(self)

    def onEnter(self, env, properties):
        """Begin creation of a package.

        The state tracker is about to witness the creation of a package. The passed
        environment, tools and (custom) properties are in their initial state that
        was inherited from the parent recipe.

        :param env: Complete environment
        :type env: Mapping[str, str]
        :param properties: All custom properties
        :type properties: Mapping[str, :class:`bob.input.PluginProperty`]
        """
        pass

    def onUse(self, downstream):
        """Use provided state of downstream package.

        This method is called if the user added the name of the state tracker
        to the ``use`` clause in the recipe. A state tracker supporting this
        notion should somehow pick up and merge the state of the downstream
        package.

        The default implementation does nothing.

        :param bob.input.PluginState downstream: State of downstream package
        """
        pass

    def onFinish(self, env, properties):
        """Finish creation of a package.

        The package was computed. The passed *env* and *properties* have their
        final state after all downstream dependencies have been resolved.

        :param env: Complete environment
        :type env: Mapping[str, str]
        :param properties: All custom properties
        :type properties: Mapping[str, :class:`bob.input.PluginProperty`]
        """
        pass


class PluginSetting:
    """Base class for plugin settings.

    Plugins can be configured in the user configuration of a project. The
    plugin must derive from this class, create an object with the default value
    and assign it to 'settings' in the plugin manifest. The default
    constructor will just store the passed value in the ``settings`` member.

    :param settings: The default settings
    """

    def __init__(self, settings):
        self.settings = settings

    def merge(self, other):
        """Merge other settings into current ones.

        This method is called when other configuration files with a higher
        precedence have been parsed. The settings in these files are first
        validated by invoking the ``validate`` static method. Then this method
        is called that should update the current object with the value of
        *other*.

        The default implementation implements the following policy:

        * Dictionaries are merged recursively on a key-by-key basis
        * Lists are appended to each other
        * Everything else in *other* reuucplaces the current settings

        It is assumed that the actual settings are stored in the ``settings``
        member variable.

        :param other: Other settings with higher precedence
        """
        if isinstance(self.settings, dict) and isinstance(other, dict):
            self.settings = updateDicRecursive(self.settings, other)
        elif isinstance(self.settings, list) and isinstance(other, list):
            self.settings = self.settings + other
        else:
            self.settings = other

    def getSettings(self):
        """Getter for settings data."""
        return self.settings

    @staticmethod
    def validate(data):
        """Validate type of settings.

        Ususally the plugin will reimplement this method and return True only
        if *data* has the expected type. The default implementation will always
        return True.

        :param data: Parsed settings data from user configuration
        :return: True if data has expected type, otherwise False.
        """
        return True


def pluginStateCompat(cls):
    """Small compat decorator to roughly support <0.15 plugins"""

    _onEnter = cls.onEnter
    _onFinish = cls.onFinish

    def onEnter(self, env, properties):
        _onEnter(self, env, {}, properties)
    def onFinish(self, env, properties):
        _onFinish(self, env, {}, properties, None)

    # wrap overridden methods
    if cls.onEnter is not PluginState.onEnter:
        cls.onEnter = onEnter
    if cls.onFinish is not PluginState.onFinish:
        cls.onFinish = onFinish

def pluginStringFunCompat(oldFun):
    def newFun(args, **kwargs):
        return oldFun(args, tools={}, **kwargs)
    return newFun


class BuiltinSetting(PluginSetting):
    """Tiny wrapper to define Bob built-in settings"""

    def __init__(self, schema, updater, mangle = False):
        self.__schema = schema
        self.__updater = updater
        self.__mangle = mangle

    def merge(self, other):
        self.__updater(self.__schema.validate(other) if self.__mangle else other)

    def validate(self, data):
        try:
            self.__schema.validate(data)
            return True
        except schema.SchemaError:
            return False

def Scm(spec, env, overrides, recipeSet):
    # resolve with environment
    spec = { k : ( env.substitute(v, "checkoutSCM::"+k) if isinstance(v, str) else v)
        for (k, v) in spec.items() }

    # apply overrides before creating scm instances. It's possible to switch the Scm type with an override..
    matchedOverrides = []
    for override in overrides:
        matched, spec = override.mangle(spec, env)
        if matched:
            matchedOverrides.append(override)

    # create scm instance
    return getScm(spec, matchedOverrides, recipeSet)

class CheckoutAssert:
    SCHEMA = schema.Schema({
        'file' : str,
        'digestSHA1' : str,
        schema.Optional('start') : int,
        schema.Optional('end') : int,
    })

    def __init__(self, spec):
        self.__file = spec['file']
        self.__digestSHA1 = spec['digestSHA1']
        self.__start = spec.get('start', 1)
        self.__end = spec.get('end', '$')

    def asScript(self):
        return dedent("""\
            COMPUTED_SHA1=$(sed -n '{START},{END}p' {FILE} | \
                    sha1sum | cut -d ' ' -f1)
            if [[ "$COMPUTED_SHA1" != "{SHA1SUM}" ]]; then
                echo "Error: CheckoutAssert {FILE} checksums did not match!" 1>&2
                echo "Specified: {SHA1SUM}" 1>&2
                echo "Now: $COMPUTED_SHA1" 1>&2
                exit 1
            fi
            """).format(START=str(self.__start),
                    END=str(self.__end),
                    FILE=self.__file, SHA1SUM=self.__digestSHA1)

    def asDigestScript(self):
        return self.__file + " " + self.__digestSHA1 + " " + str(self.__start) + " " + str(self.__end)


class CoreRef:
    __slots__ = ('__destination', '__stackAdd', '__diffTools', '__diffSandbox')

    def __init__(self, destination, stackAdd=[], diffTools={}, diffSandbox=...):
        self.__destination = destination
        self.__stackAdd = stackAdd
        self.__diffTools = diffTools
        self.__diffSandbox = diffSandbox

    def refGetDestination(self):
        return self.__destination.refGetDestination()

    def refGetStack(self):
        return self.__stackAdd + self.__destination.refGetStack()

    def refDeref(self, stack, inputTools, inputSandbox, pathFormatter, cache=None):
        if cache is None: cache = {}
        if self.__diffTools:
            tools = inputTools.copy()
            for (name, tool) in self.__diffTools.items():
                if tool is None:
                    del tools[name]
                else:
                    coreTool = cache.get(tool)
                    if coreTool is None:
                        cache[tool] = coreTool = tool.refDeref(stack, inputTools, inputSandbox, pathFormatter, cache)
                    tools[name] = coreTool
        else:
            tools = inputTools

        if self.__diffSandbox is ...:
            sandbox = inputSandbox
        elif self.__diffSandbox is None:
            sandbox = None
        elif self.__diffSandbox in cache:
            sandbox = cache[self.__diffSandbox]
        else:
            sandbox = self.__diffSandbox.refDeref(stack, inputTools, inputSandbox, pathFormatter, cache)

        return self.__destination.refDeref(stack + self.__stackAdd, tools, sandbox, pathFormatter)

class CoreItem(metaclass=ABCMeta):

    def refGetDestination(self):
        return self

    def refGetStack(self):
        return []

    @abstractmethod
    def refDeref(self, stack, inputTools, inputSandbox, pathFormatter, cache=None):
        pass


class AbstractTool:
    __slots__ = ("path", "libs", "netAccess", "environment",
        "fingerprintScript", "fingerprintIf")

    def __init__(self, spec):
        if isinstance(spec, str):
            self.path = spec
            self.libs = []
            self.netAccess = False
            self.environment = {}
            self.fingerprintScript = ""
            self.fingerprintIf = False
        else:
            self.path = spec['path']
            self.libs = spec.get('libs', [])
            self.netAccess = spec.get('netAccess', False)
            self.environment = spec.get('environment', {})
            self.fingerprintScript = spec.get('fingerprintScript', "")
            self.fingerprintIf = spec.get("fingerprintIf")

    def prepare(self, coreStepRef, env):
        """Create concrete tool for given step."""
        path = env.substitute(self.path, "provideTools::path")
        libs = [ env.substitute(l, "provideTools::libs") for l in self.libs ]
        environment = { k : env.substitute(v, "provideTools::environment::"+k)
            for k, v in self.environment.items() }
        return CoreTool(coreStepRef, path, libs, self.netAccess, environment,
                        self.fingerprintScript, self.fingerprintIf)

class CoreTool(CoreItem):
    __slots__ = ("coreStep", "path", "libs", "netAccess", "environment",
        "fingerprintScript", "fingerprintIf")

    def __init__(self, coreStep, path, libs, netAccess, environment, fingerprintScript, fingerprintIf):
        self.coreStep = coreStep
        self.path = path
        self.libs = libs
        self.netAccess = netAccess
        self.environment = environment
        self.fingerprintScript = fingerprintScript
        self.fingerprintIf = fingerprintIf

    def refDeref(self, stack, inputTools, inputSandbox, pathFormatter, cache=None):
        step = self.coreStep.refDeref(stack, inputTools, inputSandbox, pathFormatter)
        return Tool(step, self.path, self.libs, self.netAccess, self.environment,
                    self.fingerprintScript)

class Tool:
    """Representation of a tool.

    A tool is made of the result of a package, a relative path into this result
    and some optional relative library paths.
    """

    __slots__ = ("step", "path", "libs", "netAccess", "environment",
        "fingerprintScript")

    def __init__(self, step, path, libs, netAccess, environment, fingerprintScript):
        self.step = step
        self.path = path
        self.libs = libs
        self.netAccess = netAccess
        self.environment = environment
        self.fingerprintScript = fingerprintScript

    def __repr__(self):
        return "Tool({}, {}, {})".format(repr(self.step), self.path, self.libs)

    def __eq__(self, other):
        return isinstance(other, Tool) and (self.step == other.step) and (self.path == other.path) and \
            (self.libs == other.libs) and (self.netAccess == other.netAccess) and \
            (self.environment == other.environment)

    def getStep(self):
        """Return package step that produces the result holding the tool
        binaries/scripts.

        :return: :class:`bob.input.Step`
        """
        return self.step

    def getPath(self):
        """Get relative path into the result."""
        return self.path

    def getLibs(self):
        """Get list of relative library paths into the result.

        :return: List[str]
        """
        return self.libs

    def getNetAccess(self):
        """Does tool require network access?

        This reflects the `netAccess` tool property.

        :return: bool
        """
        return self.netAccess

    def getEnvironment(self):
        """Get environment variables.

        Returns the dictionary of environment variables that are defined by the
        tool.
        """
        return self.environment


class CoreSandbox(CoreItem):
    __slots__ = ("coreStep", "enabled", "paths", "mounts", "environment")

    def __init__(self, coreStep, env, enabled, spec):
        recipeSet = coreStep.corePackage.recipe.getRecipeSet()
        self.coreStep = coreStep
        self.enabled = enabled
        self.paths = recipeSet.getSandboxPaths() + spec['paths']
        self.mounts = []
        for mount in spec.get('mount', []):
            m = (env.substitute(mount[0], "provideSandbox::mount-from"),
                 env.substitute(mount[1], "provideSandbox::mount-to"),
                 mount[2])
            # silently drop empty mount lines
            if (m[0] != "") and (m[1] != ""):
                self.mounts.append(m)
        self.mounts.extend(recipeSet.getSandboxMounts())
        self.environment = {
            k : env.substitute(v, "providedSandbox::environment")
            for (k, v) in spec.get('environment', {})
        }

    def __eq__(self, other):
        return isinstance(other, CoreSandbox) and \
            (self.coreStep.variantId == other.coreStep.variantId) and \
            (self.enabled == other.enabled) and \
            (self.paths == other.paths) and \
            (self.mounts == other.mounts) and \
            (self.environment == other.environment)

    def refDeref(self, stack, inputTools, inputSandbox, pathFormatter, cache=None):
        step = self.coreStep.refDeref(stack, inputTools, inputSandbox, pathFormatter)
        return Sandbox(step, self)

class Sandbox:
    """Represents a sandbox that is used when executing a step."""

    __slots__ = ("step", "coreSandbox")

    def __init__(self, step, coreSandbox):
        self.step = step
        self.coreSandbox = coreSandbox

    def __eq__(self, other):
        return isinstance(other, Sandbox) and (self.coreSandbox == other.coreSandbox)

    def getStep(self):
        """Get the package step that yields the content of the sandbox image."""
        return self.step

    def getPaths(self):
        """Return list of global search paths.

        This is the base $PATH in the sandbox."""
        return self.coreSandbox.paths

    def getMounts(self):
        """Get custom mounts.

        This returns a list of tuples where each tuple has the format
        (hostPath, sandboxPath, options).
        """
        return self.coreSandbox.mounts

    def getEnvironment(self):
        """Get environment variables.

        Returns the dictionary of environment variables that are defined by the
        sandbox.
        """
        return self.coreSandbox.environment

    def isEnabled(self):
        """Return True if the sandbox is used in the current build configuration."""
        return self.coreSandbox.enabled


class CoreStep(CoreItem):
    __slots__ = ( "corePackage", "digestEnv", "env", "args",
        "providedEnv", "providedTools", "providedDeps", "providedSandbox",
        "variantId", "sbxVarId", "deterministic", "isValid" )

    def __init__(self, corePackage, isValid, deterministic, digestEnv, env, args):
        self.corePackage = corePackage
        self.isValid = isValid
        self.digestEnv = digestEnv.detach()
        self.env = env.detach()
        self.args = args
        self.deterministic = deterministic and all(
            arg.isDeterministic() for arg in self.getAllDepCoreSteps(True))
        self.variantId = self.getDigest(lambda coreStep: coreStep.variantId)
        self.providedEnv = {}
        self.providedTools = {}
        self.providedDeps = []
        self.providedSandbox = None

    @abstractmethod
    def getScript(self):
        pass

    @abstractmethod
    def getJenkinsScript(self):
        pass

    @abstractmethod
    def getDigestScript(self):
        pass

    @abstractmethod
    def getLabel(self):
        pass

    @abstractmethod
    def _getToolKeys(self):
        """Return relevant tool names for this CoreStep."""
        pass

    def isDeterministic(self):
        return self.deterministic

    def isCheckoutStep(self):
        return False

    def isBuildStep(self):
        return False

    def isPackageStep(self):
        return False

    def getTools(self):
        if self.isValid:
            toolKeys = self._getToolKeys()
            return { name : tool for name,tool in self.corePackage.tools.items()
                                 if name in toolKeys }
        else:
            return {}

    def getSandbox(self, forceSandbox=False):
        # Forcing the sandbox is only allowed if sandboxInvariant policy is not
        # set or disabled.
        forceSandbox = forceSandbox and \
            not self.corePackage.recipe.getRecipeSet().sandboxInvariant
        sandbox = self.corePackage.sandbox
        if sandbox and (sandbox.enabled or forceSandbox) and self.isValid:
            return sandbox
        else:
            return None

    def getAllDepCoreSteps(self, forceSandbox=False):
        sandbox = self.getSandbox(forceSandbox)
        return [ a.refGetDestination() for a in self.args ] + \
            [ d.coreStep for n,d in sorted(self.getTools().items()) ] + (
            [ sandbox.coreStep] if sandbox else [])

    def getDigest(self, calculate, forceSandbox=False):
        h = DigestHasher()
        if self.isFingerprinted() and self.getSandbox():
            h.fingerprint(DigestHasher.sliceRecipes(calculate(self.getSandbox().coreStep)))
        sandbox = not self.corePackage.recipe.getRecipeSet().sandboxInvariant and \
            self.getSandbox(forceSandbox)
        if sandbox:
            h.update(DigestHasher.sliceRecipes(calculate(sandbox.coreStep)))
            h.update(struct.pack("<I", len(sandbox.paths)))
            for p in sandbox.paths:
                h.update(struct.pack("<I", len(p)))
                h.update(p.encode('utf8'))
        else:
            h.update(b'\x00' * 20)
        script = self.getDigestScript()
        if script:
            h.update(struct.pack("<I", len(script)))
            h.update(script.encode("utf8"))
        else:
            h.update(b'\x00\x00\x00\x00')
        h.update(struct.pack("<I", len(self.getTools())))
        for (name, tool) in sorted(self.getTools().items(), key=lambda t: t[0]):
            h.update(DigestHasher.sliceRecipes(calculate(tool.coreStep)))
            h.update(struct.pack("<II", len(tool.path), len(tool.libs)))
            h.update(tool.path.encode("utf8"))
            for l in tool.libs:
                h.update(struct.pack("<I", len(l)))
                h.update(l.encode('utf8'))
        h.update(struct.pack("<I", len(self.digestEnv)))
        for (key, val) in sorted(self.digestEnv.items()):
            h.update(struct.pack("<II", len(key), len(val)))
            h.update((key+val).encode('utf8'))
        args = [ arg for arg in (a.refGetDestination() for a in self.args) if arg.isValid ]
        h.update(struct.pack("<I", len(args)))
        for arg in args:
            arg = calculate(arg)
            h.update(DigestHasher.sliceRecipes(arg))
            h.fingerprint(DigestHasher.sliceHost(arg))
        return h.digest()

    def getResultId(self):
        h = hashlib.sha1()
        h.update(self.variantId)
        # providedEnv
        h.update(struct.pack("<I", len(self.providedEnv)))
        for (key, val) in sorted(self.providedEnv.items()):
            h.update(struct.pack("<II", len(key), len(val)))
            h.update((key+val).encode('utf8'))
        # providedTools
        providedTools = self.providedTools
        h.update(struct.pack("<I", len(providedTools)))
        for (name, tool) in sorted(providedTools.items()):
            h.update(tool.coreStep.variantId)
            h.update(struct.pack("<III", len(name), len(tool.path), len(tool.libs)))
            h.update(name.encode("utf8"))
            h.update(tool.path.encode("utf8"))
            for l in tool.libs:
                h.update(struct.pack("<I", len(l)))
                h.update(l.encode('utf8'))
            h.update(struct.pack("<?I", tool.netAccess, len(tool.environment)))
            for (key, val) in sorted(tool.environment.items()):
                h.update(struct.pack("<II", len(key), len(val)))
                h.update((key+val).encode('utf8'))
            h.update(struct.pack("<I", len(tool.fingerprintScript)))
            h.update(tool.fingerprintScript.encode('utf8'))
        # provideDeps
        providedDeps = self.providedDeps
        h.update(struct.pack("<I", len(providedDeps)))
        for dep in providedDeps:
            h.update(dep.refGetDestination().variantId)
        # sandbox
        providedSandbox = self.providedSandbox
        if providedSandbox:
            h.update(providedSandbox.coreStep.variantId)
            h.update(struct.pack("<I", len(providedSandbox.paths)))
            for p in providedSandbox.paths:
                h.update(struct.pack("<I", len(p)))
                h.update(p.encode('utf8'))
            h.update(struct.pack("<I", len(providedSandbox.mounts)))
            for (mntFrom, mntTo, mntOpts) in providedSandbox.mounts:
                h.update(struct.pack("<III", len(mntFrom), len(mntTo), len(mntOpts)))
                h.update((mntFrom+mntTo+"".join(mntOpts)).encode('utf8'))
            h.update(struct.pack("<I", len(providedSandbox.environment)))
            for (key, val) in sorted(providedSandbox.environment.items()):
                h.update(struct.pack("<II", len(key), len(val)))
                h.update((key+val).encode('utf8'))
        else:
            h.update(b'\x00' * 20)

        return h.digest()

    def getSandboxVariantId(self):
        # This is a special variant to calculate the variant-id as if the
        # sandbox was enabled. This is used for live build-ids and on the
        # jenkins where the build-id of the sandbox must always be calculated.
        # But this is all obsolte if the sandboxInvariant policy is enabled.
        try:
            ret = self.sbxVarId
        except AttributeError:
            ret = self.sbxVarId = self.getDigest(
                lambda step: step.getSandboxVariantId(),
                True) if not self.corePackage.recipe.getRecipeSet().sandboxInvariant \
                      else self.variantId
        return ret

    def isFingerprinted(self):
        return not self.isCheckoutStep() and self.corePackage.fingerprintMask != 0


class Step:
    """Represents the smallest unit of execution of a package.

    A step is what gets actually executed when building packages.

    Steps can be compared and sorted. This is done based on the Variant-Id of
    the step. See :meth:`bob.input.Step.getVariantId` for details.
    """

    def __init__(self, coreStep, package, pathFormatter):
        self._coreStep = coreStep
        self.__package = package
        self.__pathFormatter = pathFormatter

    def __repr__(self):
        return "Step({}, {}, {})".format(self.getLabel(), "/".join(self.getPackage().getStack()), asHexStr(self.getVariantId()))

    def __hash__(self):
        return hash(self._coreStep.variantId)

    def __lt__(self, other):
        return self._coreStep.variantId < other._coreStep.variantId

    def __le__(self, other):
        return self._coreStep.variantId <= other._coreStep.variantId

    def __eq__(self, other):
        return self._coreStep.variantId == other._coreStep.variantId

    def __ne__(self, other):
        return self._coreStep.variantId != other._coreStep.variantId

    def __gt__(self, other):
        return self._coreStep.variantId > other._coreStep.variantId

    def __ge__(self, other):
        return self._coreStep.variantId >= other._coreStep.variantId

    def getScript(self):
        """Return a single big script of the whole step.

        Besides considerations of special backends (such as Jenkins) this
        script is what should be executed to build this step."""
        return self._coreStep.getScript()

    def getJenkinsScript(self):
        """Return the relevant parts as shell script that have no Jenkins plugin."""
        return self._coreStep.getJenkinsScript()

    def getDigestScript(self):
        """Return a long term stable script.

        The digest script will not be executed but is the basis to calculate if
        the step has changed. In case of the checkout step the involved SCMs will
        return a stable representation of _what_ is checked out and not the real
        script of _how_ this is done.
        """
        return self._coreStep.getDigestScript()

    def isDeterministic(self):
        """Return whether the step is deterministic.

        Checkout steps that have a script are considered indeterministic unless
        the recipe declares it otherwise (checkoutDeterministic). Then the SCMs
        are checked if they all consider themselves deterministic. Build and
        package steps are always deterministic.

        The determinism is defined recursively for all arguments, tools and the
        sandbox of the step too. That is, the step is only deterministic if all
        its dependencies and this step itself is deterministic.
        """
        return self._coreStep.isDeterministic()

    def isValid(self):
        """Returns True if this step is valid, False otherwise."""
        return self._coreStep.isValid

    def isCheckoutStep(self):
        """Return True if this is a checkout step."""
        return self._coreStep.isCheckoutStep()

    def isBuildStep(self):
        """Return True if this is a build step."""
        return self._coreStep.isBuildStep()

    def isPackageStep(self):
        """Return True if this is a package step."""
        return self._coreStep.isPackageStep()

    def getPackage(self):
        """Get Package object that is the parent of this Step."""
        return self.__package

    def getDigest(self, calculate, forceSandbox=False, hasher=DigestHasher, fingerprint=None):
        h = hasher()
        if self._coreStep.isFingerprinted() and self.getSandbox():
            h.fingerprint(hasher.sliceRecipes(calculate(self.getSandbox().getStep())))
        elif fingerprint:
            h.fingerprint(fingerprint)
        sandbox = not self.__package.getRecipe().getRecipeSet().sandboxInvariant and \
            self.getSandbox(forceSandbox)
        if sandbox:
            h.update(hasher.sliceRecipes(calculate(sandbox.getStep())))
            h.update(struct.pack("<I", len(sandbox.getPaths())))
            for p in sandbox.getPaths():
                h.update(struct.pack("<I", len(p)))
                h.update(p.encode('utf8'))
        else:
            h.update(b'\x00' * 20)
        script = self.getDigestScript()
        if script:
            h.update(struct.pack("<I", len(script)))
            h.update(script.encode("utf8"))
        else:
            h.update(b'\x00\x00\x00\x00')
        h.update(struct.pack("<I", len(self.getTools())))
        for (name, tool) in sorted(self.getTools().items(), key=lambda t: t[0]):
            h.update(hasher.sliceRecipes(calculate(tool.step)))
            h.update(struct.pack("<II", len(tool.path), len(tool.libs)))
            h.update(tool.path.encode("utf8"))
            for l in tool.libs:
                h.update(struct.pack("<I", len(l)))
                h.update(l.encode('utf8'))
        h.update(struct.pack("<I", len(self._coreStep.digestEnv)))
        for (key, val) in sorted(self._coreStep.digestEnv.items()):
            h.update(struct.pack("<II", len(key), len(val)))
            h.update((key+val).encode('utf8'))
        args = [ calculate(a) for a in self.getArguments() if a.isValid() ]
        h.update(struct.pack("<I", len(args)))
        for arg in args:
            h.update(hasher.sliceRecipes(arg))
            h.fingerprint(hasher.sliceHost(arg))
        return h.digest()

    async def getDigestCoro(self, calculate, forceSandbox=False, hasher=DigestHasher, fingerprint=None):
        h = hasher()
        if self._coreStep.isFingerprinted() and self.getSandbox():
            [d] = await calculate([self.getSandbox().getStep()])
            h.fingerprint(hasher.sliceRecipes(d))
        elif fingerprint:
            h.fingerprint(fingerprint)
        sandbox = not self.__package.getRecipe().getRecipeSet().sandboxInvariant and \
            self.getSandbox(forceSandbox)
        if sandbox:
            [d] = await calculate([sandbox.getStep()])
            h.update(hasher.sliceRecipes(d))
            h.update(struct.pack("<I", len(sandbox.getPaths())))
            for p in sandbox.getPaths():
                h.update(struct.pack("<I", len(p)))
                h.update(p.encode('utf8'))
        else:
            h.update(b'\x00' * 20)
        script = self.getDigestScript()
        if script:
            h.update(struct.pack("<I", len(script)))
            h.update(script.encode("utf8"))
        else:
            h.update(b'\x00\x00\x00\x00')
        h.update(struct.pack("<I", len(self.getTools())))
        tools = sorted(self.getTools().items(), key=lambda t: t[0])
        toolsDigests = await calculate([ tool.step for name,tool in tools ])
        for ((name, tool), d) in zip(tools, toolsDigests):
            h.update(hasher.sliceRecipes(d))
            h.update(struct.pack("<II", len(tool.path), len(tool.libs)))
            h.update(tool.path.encode("utf8"))
            for l in tool.libs:
                h.update(struct.pack("<I", len(l)))
                h.update(l.encode('utf8'))
        h.update(struct.pack("<I", len(self._coreStep.digestEnv)))
        for (key, val) in sorted(self._coreStep.digestEnv.items()):
            h.update(struct.pack("<II", len(key), len(val)))
            h.update((key+val).encode('utf8'))
        args = [ a for a in self.getArguments() if a.isValid() ]
        argsDigests = await calculate(args)
        h.update(struct.pack("<I", len(args)))
        for d in argsDigests:
            h.update(hasher.sliceRecipes(d))
            h.fingerprint(hasher.sliceHost(d))
        return h.digest()

    def getVariantId(self):
        """Return Variant-Id of this Step.

        The Variant-Id is used to distinguish different packages or multiple
        variants of a package. Each Variant-Id need only be built once but
        successive builds might yield different results (e.g. when building
        from branches)."""
        return self._coreStep.variantId

    def _getSandboxVariantId(self):
        return self._coreStep.getSandboxVariantId()

    def getSandbox(self, forceSandbox=False):
        """Return Sandbox used in this Step.

        Returns a Sandbox object or None if this Step is built without one.
        """
        # Forcing the sandbox is only allowed if sandboxInvariant policy is not
        # set or disabled.
        forceSandbox = forceSandbox and \
            not self.__package.getRecipe().getRecipeSet().sandboxInvariant
        sandbox = self.__package._getSandboxRaw()
        if sandbox and (sandbox.isEnabled() or forceSandbox) and self._coreStep.isValid:
            return sandbox
        else:
            return None

    def getLabel(self):
        """Return path label for step.

        This is currently defined as "src", "build" and "dist" for the
        respective steps.
        """
        return self._coreStep.getLabel()

    def getExecPath(self, referrer=None):
        """Return the execution path of the step.

        The execution path is where the step is actually run. It may be distinct
        from the workspace path if the build is performed in a sandbox. The
        ``referrer`` is an optional parameter that represents a step that refers
        to this step while building.
        """
        if self.isValid():
            return self.__pathFormatter(self, 'exec', self.__package._getStates(),
                referrer or self)
        else:
            return "/invalid/exec/path/of/{}".format(self.__package.getName())

    def getWorkspacePath(self):
        """Return the workspace path of the step.

        The workspace path represents the location of the step in the users
        workspace. When building in a sandbox this path is not passed to the
        script but the one from getExecPath() instead.
        """
        if self.isValid():
            return self.__pathFormatter(self, 'workspace', self.__package._getStates(),
                self)
        else:
            return "/invalid/workspace/path/of/{}".format(self.__package.getName())

    def getPaths(self):
        """Get sorted list of execution paths to used tools.

        The returned list is intended to be passed as PATH environment variable.
        The paths are sorted by name.
        """
        return sorted([ os.path.join(tool.step.getExecPath(self), tool.path)
            for tool in self.getTools().values() ])

    def getLibraryPaths(self):
        """Get sorted list of library paths of used tools.

        The returned list is intended to be passed as LD_LIBRARY_PATH environment
        variable. The paths are first sorted by tool name. The order of paths of
        a single tool is kept.
        """
        paths = []
        for (name, tool) in sorted(self.getTools().items()):
            paths.extend([ os.path.join(tool.step.getExecPath(self), l) for l in tool.libs ])
        return paths

    def getTools(self):
        """Get dictionary of tools.

        The dict maps the tool name to a :class:`bob.input.Tool`.
        """
        if self._coreStep.isValid:
            toolKeys = self._coreStep._getToolKeys()
            return { name : tool for name, tool in self.__package._getAllTools().items()
                                 if name in toolKeys }
        else:
            return {}

    def getArguments(self):
        """Get list of all inputs for this Step.

        The arguments are passed as absolute paths to the script starting from $1.
        """
        p = self.__package
        refCache = {}
        return [ a.refDeref(p.getStack(), p._getInputTools(), p._getInputSandboxRaw(),
                            self.__pathFormatter, refCache)
                    for a in self._coreStep.args ]

    def getAllDepSteps(self, forceSandbox=False):
        """Get all dependent steps of this Step.

        This includes the direct input to the Step as well as indirect inputs
        such as the used tools or the sandbox.
        """
        sandbox = self.getSandbox(forceSandbox)
        return self.getArguments() + [ d.step for n,d in sorted(self.getTools().items()) ] + (
            [sandbox.getStep()] if sandbox else [])

    def getEnv(self):
        """Return dict of environment variables."""
        return self._coreStep.env

    def doesProvideTools(self):
        """Return True if this step provides at least one tool."""
        return bool(self._coreStep.providedTools)

    def isShared(self):
        """Returns True if the result of the Step should be shared globally.

        The exact behaviour of a shared step/package depends on the build
        backend. In general a shared package means that the result is put into
        some shared location where it is likely that the same result is needed
        again.
        """
        return False

    def isRelocatable(self):
        """Returns True if the step is relocatable."""
        return False

    def _getProvidedDeps(self):
        p = self.__package
        refCache = {}
        return [ a.refDeref(p.getStack(), p._getInputTools(), p._getInputSandboxRaw(),
                            self.__pathFormatter, refCache)
                    for a in self._coreStep.providedDeps ]

    def _isFingerprinted(self):
        return self._coreStep.isFingerprinted()

    def _getFingerprintScript(self):
        if not self._coreStep.isFingerprinted():
            return ""

        mask = self._coreStep.corePackage.fingerprintMask
        scripts = chain(
            (t.fingerprintScript for n,t in sorted(self.getTools().items())),
            self.__package.getRecipe().fingerprintScripts)
        ret = []
        for s in scripts:
            if mask & 1: ret.append(s)
            mask >>= 1
        return mangleFingerprints(joinScripts(ret), self.getEnv())


class CoreCheckoutStep(CoreStep):
    __slots__ = ( "scmList" )

    def __init__(self, corePackage, checkout=None, fullEnv=Env(), digestEnv=Env(), env=Env()):
        if checkout:
            recipeSet = corePackage.recipe.getRecipeSet()
            overrides = recipeSet.scmOverrides()
            self.scmList = [ Scm(scm, fullEnv, overrides, recipeSet)
                for scm in checkout[2]
                if fullEnv.evaluate(scm.get("if"), "checkoutSCM") ]
            isValid = (checkout[0] is not None) or bool(self.scmList)

            # Validate that SCM paths do not overlap
            knownPaths = []
            for s in self.scmList:
                p = s.getDirectory()
                if os.path.isabs(p):
                    raise ParseError("SCM paths must be relative! Offending path: " + p)
                for known in knownPaths:
                    if overlappingPaths(known, p):
                        raise ParseError("SCM paths '{}' and '{}' overlap."
                                            .format(known, p))
                knownPaths.append(p)
        else:
            isValid = False
            self.scmList = []

        deterministic = corePackage.recipe.checkoutDeterministic
        super().__init__(corePackage, isValid, deterministic, digestEnv, env, [])

    def _getToolKeys(self):
        return self.corePackage.recipe.toolDepCheckout

    def refDeref(self, stack, inputTools, inputSandbox, pathFormatter, cache=None):
        package = self.corePackage.refDeref(stack, inputTools, inputSandbox, pathFormatter)
        ret = CheckoutStep(self, package, pathFormatter)
        package._setCheckoutStep(ret)
        return ret

    def getLabel(self):
        return "src"

    def isDeterministic(self):
        return super().isDeterministic() and all(s.isDeterministic() for s in self.scmList)

    def hasLiveBuildId(self):
        return super().isDeterministic() and all(s.hasLiveBuildId() for s in self.scmList)

    def isCheckoutStep(self):
        return True

    def getScript(self):
        recipe = self.corePackage.recipe
        return joinScripts([s.asScript() for s in self.scmList]
                + [recipe.checkoutScript]
                + [s.asScript() for s in recipe.checkoutAsserts])

    def getJenkinsScript(self):
        recipe = self.corePackage.recipe
        return joinScripts([ s.asScript() for s in self.scmList if not s.hasJenkinsPlugin() ]
            + [recipe.checkoutScript]
            + [s.asScript() for s in recipe.checkoutAsserts])

    def getDigestScript(self):
        if self.isValid:
            recipe = self.corePackage.recipe
            return "\n".join([s.asDigestScript() for s in self.scmList]
                    + [recipe.checkoutDigestScript]
                    + [s.asDigestScript() for s in recipe.checkoutAsserts])
        else:
            return None

class CheckoutStep(Step):
    def getJenkinsXml(self, credentials, options):
        return [ s.asJenkins(self.getWorkspacePath(), credentials, options)
                 for s in self._coreStep.scmList if s.hasJenkinsPlugin() ]

    def getScmList(self):
        return self._coreStep.scmList

    def getScmDirectories(self):
        dirs = {}
        for s in self._coreStep.scmList:
            dirs[s.getDirectory()] = (hashString(s.asDigestScript()), s.getProperties())
        return dirs

    def hasLiveBuildId(self):
        """Check if live build-ids are supported.

        This must be supported by all SCMs. Additionally the checkout script
        must be deterministic.
        """
        return self._coreStep.hasLiveBuildId()

    async def predictLiveBuildId(self):
        """Query server to predict live build-id.

        Returns the live-build-id or None if an SCM query failed.
        """
        if not self.hasLiveBuildId():
            return None
        h = hashlib.sha1()
        h.update(self._getSandboxVariantId())
        for s in self._coreStep.scmList:
            liveBId = await s.predictLiveBuildId(self)
            if liveBId is None: return None
            h.update(liveBId)
        return h.digest()

    def calcLiveBuildId(self):
        """Calculate live build-id from workspace."""
        if not self.hasLiveBuildId():
            return None
        workspacePath = self.getWorkspacePath()
        h = hashlib.sha1()
        h.update(self._getSandboxVariantId())
        for s in self._coreStep.scmList:
            liveBId = s.calcLiveBuildId(workspacePath)
            if liveBId is None: return None
            h.update(liveBId)
        return h.digest()

    def getLiveBuildIdSpec(self):
        """Generate spec lines for bob-hash-engine.

        May return None if an SCM does not support live-build-ids on Jenkins.
        """
        if not self.hasLiveBuildId():
            return None
        workspacePath = self.getWorkspacePath()
        lines = [ "{sha1", "=" + asHexStr(self._getSandboxVariantId()) ]
        for s in self._coreStep.scmList:
            liveBIdSpec = s.getLiveBuildIdSpec(workspacePath)
            if liveBIdSpec is None: return None
            lines.append(liveBIdSpec)
        lines.append("}")
        return "\n".join(lines)

    def hasNetAccess(self):
        return True


class CoreBuildStep(CoreStep):

    def __init__(self, corePackage, script=(None, None), digestEnv=Env(), env=Env(), args=[]):
        isValid = script[0] is not None
        super().__init__(corePackage, isValid, True, digestEnv, env, args)

    def _getToolKeys(self):
        return self.corePackage.recipe.toolDepBuild

    def refDeref(self, stack, inputTools, inputSandbox, pathFormatter, cache=None):
        package = self.corePackage.refDeref(stack, inputTools, inputSandbox, pathFormatter)
        ret = BuildStep(self, package, pathFormatter)
        package._setBuildStep(ret)
        return ret

    def getLabel(self):
        return "build"

    def isBuildStep(self):
        return True

    def getScript(self):
        return self.corePackage.recipe.buildScript

    def getJenkinsScript(self):
        return self.corePackage.recipe.buildScript

    def getDigestScript(self):
        return self.corePackage.recipe.buildDigestScript

class BuildStep(Step):

    def hasNetAccess(self):
        return self.getPackage().getRecipe()._getBuildNetAccess() or any(
            t.getNetAccess() for t in self.getTools().values())


class CorePackageStep(CoreStep):

    def __init__(self, corePackage, script=(None, None), digestEnv=Env(), env=Env(), args=[]):
        isValid = script[0] is not None
        super().__init__(corePackage, isValid, True, digestEnv, env, args)

    def _getToolKeys(self):
        return self.corePackage.recipe.toolDepPackage

    def refDeref(self, stack, inputTools, inputSandbox, pathFormatter, cache=None):
        package = self.corePackage.refDeref(stack, inputTools, inputSandbox, pathFormatter)
        ret = PackageStep(self, package, pathFormatter)
        package._setPackageStep(ret)
        return ret

    def getLabel(self):
        return "dist"

    def isPackageStep(self):
        return True

    def getScript(self):
        return self.corePackage.recipe.packageScript

    def getJenkinsScript(self):
        return self.corePackage.recipe.packageScript

    def getDigestScript(self):
        return self.corePackage.recipe.packageDigestScript

class PackageStep(Step):

    def isShared(self):
        return self.getPackage().getRecipe().isShared()

    def isRelocatable(self):
        """Returns True if the package step is relocatable."""
        return self.getPackage().isRelocatable()

    def hasNetAccess(self):
        return self.getPackage().getRecipe()._getPackageNetAccess() or any(
            t.getNetAccess() for t in self.getTools().values())


class CorePackageInternal(CoreItem):
    __slots__ = []
    def refDeref(self, stack, inputTools, inputSandbox, pathFormatter, cache=None):
        return (inputTools, inputSandbox)

corePackageInternal = CorePackageInternal()

class CorePackage:
    __slots__ = ("recipe", "internalRef", "directDepSteps", "indirectDepSteps",
        "states", "tools", "sandbox", "checkoutStep", "buildStep", "packageStep",
        "pkgId", "fingerprintMask")

    def __init__(self, recipe, tools, diffTools, sandbox, diffSandbox,
                 directDepSteps, indirectDepSteps, states, pkgId, fingerprintMask):
        self.recipe = recipe
        self.tools = tools
        self.sandbox = sandbox
        self.internalRef = CoreRef(corePackageInternal, [], diffTools, diffSandbox)
        self.directDepSteps = directDepSteps
        self.indirectDepSteps = indirectDepSteps
        self.states = states
        self.pkgId = pkgId
        self.fingerprintMask = fingerprintMask

    def refDeref(self, stack, inputTools, inputSandbox, pathFormatter):
        tools, sandbox = self.internalRef.refDeref(stack, inputTools, inputSandbox, pathFormatter)
        return Package(self, stack, pathFormatter, inputTools, tools, inputSandbox, sandbox)

    def createCoreCheckoutStep(self, checkout, fullEnv, digestEnv, env):
        ret = self.checkoutStep = CoreCheckoutStep(self, checkout, fullEnv, digestEnv, env)
        return ret

    def createInvalidCoreCheckoutStep(self):
        ret = self.checkoutStep = CoreCheckoutStep(self)
        return ret

    def createCoreBuildStep(self, script, digestEnv, env, args):
        ret = self.buildStep = CoreBuildStep(self, script, digestEnv, env, args)
        return ret

    def createInvalidCoreBuildStep(self):
        ret = self.buildStep = CoreBuildStep(self)
        return ret

    def createCorePackageStep(self, script, digestEnv, env, args):
        ret = self.packageStep = CorePackageStep(self, script, digestEnv, env, args)
        return ret

    def getCorePackageStep(self):
        return self.packageStep

    def getName(self):
        """Name of the package"""
        return self.recipe.getPackageName()

class Package(object):
    """Representation of a package that was created from a recipe.

    Usually multiple packages will be created from a single recipe. This is
    either due to multiple upstream recipes or different variants of the same
    package. This does not preclude the possibility that multiple Package
    objects describe exactly the same package (read: same Variant-Id). It is
    the responsibility of the build backend to detect this and build only one
    package.
    """

    def __init__(self, corePackage, stack, pathFormatter, inputTools, tools, inputSandbox, sandbox):
        self.__corePackage = corePackage
        self.__stack = stack
        self.__pathFormatter = pathFormatter
        self.__inputTools = inputTools
        self.__tools = tools
        self.__inputSandbox = inputSandbox
        self.__sandbox = sandbox

    def __eq__(self, other):
        return isinstance(other, Package) and (self.__stack == other.__stack)

    def _getId(self):
        """The package-Id is uniquely representing every package variant.

        On the package level there might be more dependencies than on the step
        level. Meta variables are usually unused and also do not contribute to
        the variant-id. The package-id still guarantees to not collide in these
        cases. OTOH there can be identical packages with different ids, though
        it should be an unusual case.
        """
        return self.__corePackage.pkgId

    def _getInputTools(self):
        return self.__inputTools

    def _getAllTools(self):
        return self.__tools

    def _getInputSandboxRaw(self):
        return self.__inputSandbox

    def _getSandboxRaw(self):
        return self.__sandbox

    def getName(self):
        """Name of the package"""
        return self.getRecipe().getPackageName()

    def getMetaEnv(self):
        """meta variables of package"""
        return self.getRecipe().getMetaEnv()

    def getStack(self):
        """Returns the recipe processing stack leading to this package.

        The method returns a list of package names. The first entry is a root
        recipe and the last entry is this package."""
        return self.__stack

    def getRecipe(self):
        """Return Recipe object that was the template for this package."""
        return self.__corePackage.recipe

    def getDirectDepSteps(self):
        """Return list to the package steps of the direct dependencies.

        Direct dependencies are the ones that are named explicitly in the
        ``depends`` section of the recipe. The order of the items is
        preserved from the recipe.
        """
        refCache = {}
        return [ d.refDeref(self.__stack, self.__inputTools, self.__inputSandbox,
                            self.__pathFormatter, refCache)
                    for d in self.__corePackage.directDepSteps ]

    def getIndirectDepSteps(self):
        """Return list of indirect dependencies of the package.

        Indirect dependencies are dependencies that were provided by downstream
        recipes. They are not directly named in the recipe.
        """
        refCache = {}
        return [ d.refDeref(self.__stack, self.__inputTools, self.__inputSandbox,
                            self.__pathFormatter, refCache)
                    for d in self.__corePackage.indirectDepSteps ]

    def getAllDepSteps(self, forceSandbox=False):
        """Return list of all dependencies of the package.

        This list includes all direct and indirect dependencies. Additionally
        the used sandbox and tools are included too."""
        # Forcing the sandbox is only allowed if sandboxInvariant policy is not
        # set or disabled.
        forceSandbox = forceSandbox and \
            not self.getRecipe().getRecipeSet().sandboxInvariant
        allDeps = set(self.getDirectDepSteps())
        allDeps |= set(self.getIndirectDepSteps())
        if self.__sandbox and (self.__sandbox.isEnabled() or forceSandbox):
            allDeps.add(self.__sandbox.getStep())
        for i in self.getPackageStep().getTools().values(): allDeps.add(i.getStep())
        return sorted(allDeps)

    def _setCheckoutStep(self, checkoutStep):
        self.__checkoutStep = checkoutStep

    def getCheckoutStep(self):
        """Return the checkout step of this package."""
        try:
            ret = self.__checkoutStep
        except AttributeError:
            ret = self.__checkoutStep = CheckoutStep(self.__corePackage.checkoutStep,
                self, self.__pathFormatter)
        return ret

    def _setBuildStep(self, buildStep):
        self.__buildStep = buildStep

    def getBuildStep(self):
        """Return the build step of this package."""
        try:
            ret = self.__buildStep
        except AttributeError:
            ret = self.__buildStep = BuildStep(self.__corePackage.buildStep,
                self, self.__pathFormatter)
        return ret

    def _setPackageStep(self, packageStep):
        self.__packageStep = packageStep

    def getPackageStep(self):
        """Return the package step of this package."""
        try:
            ret = self.__packageStep
        except AttributeError:
            ret = self.__packageStep = PackageStep(self.__corePackage.packageStep,
                self, self.__pathFormatter)
        return ret

    def _getStates(self):
        return self.__corePackage.states

    def isRelocatable(self):
        """Returns True if the packages is relocatable."""
        return self.__corePackage.recipe.isRelocatable()


# FIXME: implement this on our own without the Template class. How to do proper
# escaping?
class IncludeHelper:

    class Resolver:
        def __init__(self, fileLoader, baseDir, varBase, origText):
            self.fileLoader = fileLoader
            self.baseDir = baseDir
            self.varBase = varBase
            self.prolog = []
            self.incDigests = [ asHexStr(hashlib.sha1(origText.encode('utf8')).digest()) ]
            self.count = 0

        def __getitem__(self, item):
            mode = item[0]
            item = item[1:]
            content = []
            try:
                paths = sorted(glob(os.path.join(self.baseDir, item)))
                if not paths:
                    raise ParseError("No files matched in include pattern '{}'!"
                        .format(item))
                for path in paths:
                    content.append(self.fileLoader(path))
            except OSError as e:
                raise ParseError("Error including '"+item+"': " + str(e))
            content = b''.join(content)

            self.incDigests.append(asHexStr(hashlib.sha1(content).digest()))
            if mode == '<':
                var = "_{}{}".format(self.varBase, self.count)
                self.count += 1
                self.prolog.extend([
                    "{VAR}=$(mktemp)".format(VAR=var),
                    "_BOB_TMP_CLEANUP+=( ${VAR} )".format(VAR=var),
                    "base64 -d > ${VAR} <<EOF".format(VAR=var)])
                self.prolog.extend(sliceString(b64encode(content).decode("ascii"), 76))
                self.prolog.append("EOF")
                ret = "${" + var + "}"
            else:
                assert mode == "'"
                ret = quote(content.decode('utf8'))

            return ret

    def __init__(self, fileLoader, baseDir, varBase, sourceName):
        self.__pattern = re.compile(r"""
            \$<(?:
                (?P<escaped>\$)     |
                (?P<named>[<'][^'>]+)['>]>  |
                (?P<braced>[<'][^'>]+)['>]> |
                (?P<invalid>)
            )
            """, re.VERBOSE)
        self.__baseDir = baseDir
        self.__varBase = re.sub(r'[^a-zA-Z0-9_]', '_', varBase, flags=re.DOTALL)
        self.__fileLoader = fileLoader
        self.__sourceName = sourceName

    def resolve(self, text, section):
        if isinstance(text, str):
            resolver = IncludeHelper.Resolver(self.__fileLoader, self.__baseDir, self.__varBase, text)
            t = Template(text)
            t.delimiter = '$<'
            t.pattern = self.__pattern
            try:
                ret = t.substitute(resolver)
            except ValueError as e:
                raise ParseError("Bad substiturion in {}: {}".format(section, str(e)))
            sourceAnchor = "_BOB_SOURCES[$LINENO]=" + quote(self.__sourceName)
            return ("\n".join(resolver.prolog + [sourceAnchor, ret]), "\n".join(resolver.incDigests))
        else:
            return (None, None)

def mergeFilter(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return left + right

class ScmValidator:
    def __init__(self, scmSpecs):
        self.__scmSpecs = scmSpecs

    def __validateScm(self, scm):
        if 'scm' not in scm:
            raise schema.SchemaMissingKeyError("Missing 'scm' key in {}".format(scm), None)
        if scm['scm'] not in self.__scmSpecs.keys():
            raise schema.SchemaWrongKeyError('Invalid SCM: {}'.format(scm['scm']), None)
        self.__scmSpecs[scm['scm']].validate(scm)

    def validate(self, data):
        if isinstance(data, dict):
            self.__validateScm(data)
        elif isinstance(data, list):
            for i in data: self.__validateScm(i)
        else:
            raise schema.SchemaUnexpectedTypeError(
                'checkoutSCM must be a SCM spec or a list threreof',
                None)
        return data

class VarDefineValidator:
    def __init__(self, keyword):
        self.__varName = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
        self.__keyword = keyword

    def validate(self, data):
        if not isinstance(data, dict):
            raise schema.SchemaUnexpectedTypeError(
                "{}: must be a dictionary".format(self.__keyword), None)
        for key,value in sorted(data.items()):
            if not isinstance(key, str):
                raise schema.SchemaUnexpectedTypeError(
                    "{}: bad variable '{}'. Environment variable names must be strings!"
                        .format(self.__keyword, key),
                    None)
            if key.startswith("BOB_"):
                raise schema.SchemaWrongKeyError(
                    "{}: bad variable '{}'. Environment variables starting with 'BOB_' are reserved!"
                        .format(self.__keyword, key),
                    None)
            if self.__varName.match(key) is None:
                raise schema.SchemaWrongKeyError(
                    "{}: bad variable name '{}'.".format(self.__keyword, key),
                    None)
            if not isinstance(value, str):
                raise schema.SchemaUnexpectedTypeError(
                    "{}: bad variable '{}'. Environment variable values must be strings!"
                        .format(self.__keyword, key),
                    None)
        return data


RECIPE_NAME_SCHEMA = schema.Regex(r'^[0-9A-Za-z_.+-]+$')
MULTIPACKAGE_NAME_SCHEMA = schema.Regex(r'^[0-9A-Za-z_.+-]*$')

class UniquePackageList:
    def __init__(self, stack, errorHandler):
        self.stack = stack
        self.errorHandler = errorHandler
        self.ret = []
        self.cache = {}

    def append(self, ref):
        step = ref.refGetDestination()
        name = step.corePackage.getName()
        ref2 = self.cache.get(name)
        if ref2 is None:
            self.cache[name] = ref
            self.ret.append(ref)
        elif ref2.refGetDestination().variantId != step.variantId:
            self.errorHandler(name, self.stack + ref.refGetStack(), self.stack + ref2.refGetStack())

    def extend(self, gen):
        for i in gen: self.append(i)

    def result(self):
        return self.ret

class DepTracker:

    __slots__ = ('item', 'isNew', 'usedResult')

    def __init__(self, item):
        self.item = item
        self.isNew = True
        self.usedResult = False

    def prime(self):
        if self.isNew:
            self.isNew = False
            return True
        else:
            return False

    def useResultOnce(self):
        if self.usedResult:
            return False
        else:
            self.usedResult = True
            return True

class Recipe(object):
    """Representation of a single recipe

    Multiple instaces of this class will be created if the recipe used the
    ``multiPackage`` keyword.  In this case the getName() method will return
    the name of the original recipe but the getPackageName() method will return
    it with some addition suffix. Without a ``multiPackage`` keyword there will
    only be one Recipe instance.
    """

    class Dependency(object):
        def __init__(self, recipe, env, fwd, use, cond):
            self.recipe = recipe
            self.envOverride = env
            self.provideGlobal = fwd
            self.use = use
            self.useEnv = "environment" in self.use
            self.useTools = "tools" in self.use
            self.useBuildResult = "result" in self.use
            self.useDeps = "deps" in self.use
            self.useSandbox = "sandbox" in self.use
            self.condition = cond

        @staticmethod
        def __parseEntry(dep, env, fwd, use, cond):
            if isinstance(dep, str):
                return [ Recipe.Dependency(dep, env, fwd, use, cond) ]
            else:
                envOverride = dep.get("environment")
                if envOverride:
                    env = env.copy()
                    env.update(envOverride)
                fwd = dep.get("forward", fwd)
                use = dep.get("use", use)
                newCond = dep.get("if")
                if newCond is not None:
                    cond = "$(and,{},{})".format(cond, newCond) if cond is not None else newCond
                name = dep.get("name")
                if name:
                    if "depends" in dep:
                        raise ParseError("A dependency must not use 'name' and 'depends' at the same time!")
                    return [ Recipe.Dependency(name, env, fwd, use, cond) ]
                dependencies = dep.get("depends")
                if dependencies is None:
                    raise ParseError("Either 'name' or 'depends' required for dependencies!")
                return Recipe.Dependency.parseEntries(dependencies, env, fwd, use, cond)

        @staticmethod
        def parseEntries(deps, env={}, fwd=False, use=["result", "deps"], cond=None):
            """Returns an iterator yielding all dependencies as flat list"""
            # return flattened list of dependencies
            return chain.from_iterable(
                Recipe.Dependency.__parseEntry(dep, env, fwd, use, cond)
                for dep in deps )

    @staticmethod
    def loadFromFile(recipeSet, layer, rootDir, fileName, properties, fileSchema, isRecipe):
        # MultiPackages are handled as separate recipes with an anonymous base
        # class. Directories are treated as categories separated by '::'.
        baseName = os.path.splitext( fileName )[0].split( os.sep )
        fileName = os.path.join(rootDir, fileName)
        try:
            for n in baseName: RECIPE_NAME_SCHEMA.validate(n)
        except schema.SchemaError as e:
            raise ParseError("Invalid recipe name: '{}'".format(fileName))
        baseName = "::".join( baseName )
        baseDir = os.path.dirname(fileName)

        nameMap = {}
        def anonNameCalculator(suffix):
            num = nameMap.setdefault(suffix, 0) + 1
            nameMap[suffix] = num
            return baseName + suffix + "#" + str(num)

        def collect(recipe, suffix, anonBaseClass):
            if "multiPackage" in recipe:
                anonBaseClass = Recipe(recipeSet, recipe, layer, fileName, baseDir,
                    anonNameCalculator(suffix), baseName, properties, isRecipe,
                    anonBaseClass)
                return chain.from_iterable(
                    collect(subSpec, suffix + ("-"+subName if subName else ""),
                            anonBaseClass)
                    for (subName, subSpec) in recipe["multiPackage"].items() )
            else:
                packageName = baseName + suffix
                return [ Recipe(recipeSet, recipe, layer, fileName, baseDir, packageName,
                                baseName, properties, isRecipe, anonBaseClass) ]

        return list(collect(recipeSet.loadYaml(fileName, fileSchema), "", None))

    @staticmethod
    def createVirtualRoot(recipeSet, roots, properties):
        recipe = {
            "depends" : [
                { "name" : name, "use" : ["result"] } for name in roots
            ],
            "buildScript" : "true",
            "packageScript" : "true"
        }
        ret = Recipe(recipeSet, recipe, [], "", ".", "", "", properties)
        ret.resolveClasses()
        return ret

    def __init__(self, recipeSet, recipe, layer, sourceFile, baseDir, packageName, baseName,
                 properties, isRecipe=True, anonBaseClass=None):
        self.__recipeSet = recipeSet
        self.__sources = [ sourceFile ] if anonBaseClass is None else []
        self.__classesResolved = False
        self.__inherit = recipe.get("inherit", [])
        self.__anonBaseClass = anonBaseClass
        self.__deps = list(Recipe.Dependency.parseEntries(recipe.get("depends", [])))
        filt = recipe.get("filter", {})
        if filt: warnFilter.warn(baseName)
        self.__filterEnv = maybeGlob(filt.get("environment"))
        self.__filterTools = maybeGlob(filt.get("tools"))
        self.__filterSandbox = maybeGlob(filt.get("sandbox"))
        self.__packageName = packageName
        self.__baseName = baseName
        self.__root = recipe.get("root")
        self.__provideTools = { name : AbstractTool(spec)
            for (name, spec) in recipe.get("provideTools", {}).items() }
        self.__provideVars = recipe.get("provideVars", {})
        self.__provideDeps = set(recipe.get("provideDeps", []))
        self.__provideSandbox = recipe.get("provideSandbox")
        self.__varSelf = recipe.get("environment", {})
        self.__varPrivate = recipe.get("privateEnvironment", {})
        self.__metaEnv = recipe.get("metaEnvironment", {})
        self.__checkoutVars = set(recipe.get("checkoutVars", []))
        self.__checkoutVarsWeak = set(recipe.get("checkoutVarsWeak", []))
        self.__buildVars = set(recipe.get("buildVars", []))
        self.__buildVars |= self.__checkoutVars
        self.__buildVarsWeak = set(recipe.get("buildVarsWeak", []))
        self.__buildVarsWeak |= self.__checkoutVarsWeak
        self.__packageVars = set(recipe.get("packageVars", []))
        self.__packageVars |= self.__buildVars
        self.__packageVarsWeak = set(recipe.get("packageVarsWeak", []))
        self.__packageVarsWeak |= self.__buildVarsWeak
        self.__toolDepCheckout = set(recipe.get("checkoutTools", []))
        self.__toolDepBuild = set(recipe.get("buildTools", []))
        self.__toolDepBuild |= self.__toolDepCheckout
        self.__toolDepPackage = set(recipe.get("packageTools", []))
        self.__toolDepPackage |= self.__toolDepBuild
        self.__shared = recipe.get("shared")
        self.__relocatable = recipe.get("relocatable")
        self.__properties = {
            n : p(n in recipe, recipe.get(n))
            for (n, p) in properties.items()
        }
        self.__corePackagesByMatch = []
        self.__corePackagesById = {}

        sourceName = ("Recipe " if isRecipe else "Class  ") + packageName + (
            ", layer "+"/".join(layer) if layer else "")
        incHelper = IncludeHelper(recipeSet.loadBinary, baseDir, packageName,
                                  sourceName)

        (checkoutScript, checkoutDigestScript) = incHelper.resolve(recipe.get("checkoutScript"), "checkoutScript")
        checkoutSCMs = recipe.get("checkoutSCM", [])
        if isinstance(checkoutSCMs, dict):
            checkoutSCMs = [checkoutSCMs]
        elif not isinstance(checkoutSCMs, list):
            raise ParseError("checkoutSCM must be a dict or a list")
        i = 0
        for scm in checkoutSCMs:
            scm["__source"] = sourceName
            scm["recipe"] = "{}#{}".format(sourceFile, i)
            i += 1
        checkoutAsserts = recipe.get("checkoutAssert", [])
        self.__checkout = (checkoutScript, checkoutDigestScript, checkoutSCMs, checkoutAsserts)
        self.__build = incHelper.resolve(recipe.get("buildScript"), "buildScript")
        self.__package = incHelper.resolve(recipe.get("packageScript"), "packageScript")
        fingerprintScript = recipe.get("fingerprintScript")
        fingerprintIf = recipe.get("fingerprintIf", None if fingerprintScript else False)
        if fingerprintIf != False:
            self.__fingerprintScripts = [ fingerprintScript ]
            self.__fingerprintIf = [ fingerprintIf ]
        else:
            self.__fingerprintScripts = []
            self.__fingerprintIf = []

        # Consider checkout deterministic by default if no checkout script is
        # involved.
        self.__checkoutDeterministic = recipe.get("checkoutDeterministic", checkoutScript is None)

        self.__buildNetAccess = recipe.get("buildNetAccess")
        self.__packageNetAccess = recipe.get("packageNetAccess")

    def __resolveClassesOrder(self, cls, stack, visited, isRecipe=False):
        # prevent cycles
        clsName = "<recipe>" if isRecipe else cls.__packageName
        if clsName in stack:
            raise ParseError("Cyclic class inheritence: " + " -> ".join(stack + [clsName]))

        # depth first
        ret = []
        subInherit = [ self.__recipeSet.getClass(c) for c in cls.__inherit ]
        if cls.__anonBaseClass: subInherit.insert(0, cls.__anonBaseClass)
        for c in subInherit:
            ret.extend(self.__resolveClassesOrder(c, stack + [clsName], visited))

        # classes are inherited only once
        if (clsName not in visited) and not isRecipe:
            ret.append(cls)
            visited.add(clsName)

        return ret

    def resolveClasses(self):
        # must be done only once
        if self.__classesResolved: return
        self.__classesResolved = True

        # calculate order of classes (depth first) but ignore ourself
        inherit = self.__resolveClassesOrder(self, [], set(), True)

        # prepare environment merge list
        mergeEnvironment = self.__recipeSet.getPolicy('mergeEnvironment')
        if mergeEnvironment:
            self.__varSelf = [ self.__varSelf ] if self.__varSelf else []
            self.__varPrivate = [ self.__varPrivate ] if self.__varPrivate else []

        # inherit classes
        inherit.reverse()
        for cls in inherit:
            self.__sources.extend(cls.__sources)
            self.__deps[0:0] = cls.__deps
            self.__filterEnv = mergeFilter(self.__filterEnv, cls.__filterEnv)
            self.__filterTools = mergeFilter(self.__filterTools, cls.__filterTools)
            self.__filterSandbox = mergeFilter(self.__filterSandbox, cls.__filterSandbox)
            if self.__root is None: self.__root = cls.__root
            if self.__shared is None: self.__shared = cls.__shared
            if self.__relocatable is None: self.__relocatable = cls.__relocatable
            tmp = cls.__provideTools.copy()
            tmp.update(self.__provideTools)
            self.__provideTools = tmp
            tmp = cls.__provideVars.copy()
            tmp.update(self.__provideVars)
            self.__provideVars = tmp
            self.__provideDeps |= cls.__provideDeps
            if self.__provideSandbox is None: self.__provideSandbox = cls.__provideSandbox
            if mergeEnvironment:
                if cls.__varSelf: self.__varSelf.insert(0, cls.__varSelf)
                if cls.__varPrivate: self.__varPrivate.insert(0, cls.__varPrivate)
            else:
                tmp = cls.__varSelf.copy()
                tmp.update(self.__varSelf)
                self.__varSelf = tmp
                tmp = cls.__varPrivate.copy()
                tmp.update(self.__varPrivate)
                self.__varPrivate = tmp
            self.__checkoutVars |= cls.__checkoutVars
            tmp = cls.__metaEnv.copy()
            tmp.update(self.__metaEnv)
            self.__metaEnv = tmp
            self.__checkoutVarsWeak |= cls.__checkoutVarsWeak
            self.__buildVars |= cls.__buildVars
            self.__buildVarsWeak |= cls.__buildVarsWeak
            self.__packageVars |= cls.__packageVars
            self.__packageVarsWeak |= cls.__packageVarsWeak
            self.__toolDepCheckout |= cls.__toolDepCheckout
            self.__toolDepBuild |= cls.__toolDepBuild
            self.__toolDepPackage |= cls.__toolDepPackage
            (checkoutScript, checkoutDigestScript, checkoutSCMs, checkoutAsserts) = self.__checkout
            self.__checkoutDeterministic = self.__checkoutDeterministic and cls.__checkoutDeterministic
            if self.__buildNetAccess is None: self.__buildNetAccess = cls.__buildNetAccess
            if self.__packageNetAccess is None: self.__packageNetAccess = cls.__packageNetAccess
            # merge scripts
            checkoutScript = joinScripts([cls.__checkout[0], checkoutScript])
            checkoutDigestScript = joinScripts([cls.__checkout[1], checkoutDigestScript], "\n")
            # merge SCMs
            scms = cls.__checkout[2][:]
            scms.extend(checkoutSCMs)
            checkoutSCMs = scms
            # merge CheckoutAsserts
            casserts = cls.__checkout[3][:]
            casserts.extend(checkoutAsserts)
            checkoutAsserts = casserts
            # store result
            self.__checkout = (checkoutScript, checkoutDigestScript, checkoutSCMs, checkoutAsserts)
            self.__build = (
                joinScripts([cls.__build[0], self.__build[0]]),
                joinScripts([cls.__build[1], self.__build[1]], "\n")
            )
            self.__package = (
                joinScripts([cls.__package[0], self.__package[0]]),
                joinScripts([cls.__package[1], self.__package[1]], "\n")
            )
            self.__fingerprintScripts[0:0] = cls.__fingerprintScripts
            self.__fingerprintIf[0:0] = cls.__fingerprintIf
            for (n, p) in self.__properties.items():
                p.inherit(cls.__properties[n])

        # finalize environment merge list
        if not mergeEnvironment:
            self.__varSelf = [ self.__varSelf ] if self.__varSelf else []
            self.__varPrivate = [ self.__varPrivate ] if self.__varPrivate else []

        # the package step must always be valid
        if self.__package[0] is None:
            self.__package = ("", 'da39a3ee5e6b4b0d3255bfef95601890afd80709')

        # final shared value
        self.__shared = self.__shared == True

        # Either 'relocatable' was set in the recipe/class(es) or it defaults
        # to True unless a tool is defined. This was the legacy behaviour
        # before Bob 0.14. If the allRelocatable policy is enabled we always
        # default to True.
        if self.__relocatable is None:
            self.__relocatable = self.__recipeSet.getPolicy('allRelocatable') \
                or not self.__provideTools

        # check provided dependencies
        availDeps = [ d.recipe for d in self.__deps ]
        providedDeps = set()
        for pattern in self.__provideDeps:
            l = set(d for d in availDeps if fnmatch.fnmatchcase(d, pattern))
            if not l:
                raise ParseError("Unknown dependency '{}' in provideDeps".format(pattern))
            providedDeps |= l
        self.__provideDeps = providedDeps

    def getRecipeSet(self):
        return self.__recipeSet

    def getSources(self):
        return self.__sources

    def getPackageName(self):
        """Get the name of the package that is drived from this recipe.

        Usually the package name is the same as the recipe name. But in case of
        a ``multiPackage`` the package name has an additional suffix.
        """
        return self.__packageName

    def getName(self):
        """Get plain recipe name.

        In case of a ``multiPackage`` multiple packages may be derived from the
        same recipe. This method returns the plain recipe name.
        """
        return self.__baseName

    def getMetaEnv(self):
        return self.__metaEnv

    def isRoot(self):
        """Returns True if this is a root recipe."""
        return self.__root == True

    def isRelocatable(self):
        """Returns True if the packages of this recipe are relocatable."""
        return self.__relocatable

    def isShared(self):
        return self.__shared

    def prepare(self, inputEnv, sandboxEnabled, inputStates, inputSandbox=None,
                inputTools=Env(), stack=[]):
        # already calculated?
        for m in self.__corePackagesByMatch:
            if m.matches(inputEnv.detach(), inputTools.detach(), inputStates, inputSandbox):
                if set(stack) & m.subTreePackages:
                    raise ParseError("Recipes are cyclic")
                m.touch(inputEnv, inputTools)
                if DEBUG['pkgck']:
                    reusedCorePackage = m.corePackage
                    break
                return m.corePackage, m.subTreePackages
        else:
            reusedCorePackage = None

        # Track tool and sandbox changes
        diffSandbox = ...
        diffTools = { }

        # make copies because we will modify them
        sandbox = inputSandbox
        if self.__filterTools is None:
            inputTools = inputTools.copy()
        else:
            oldInputTools = set(inputTools.inspect().keys())
            inputTools = inputTools.filter(self.__filterTools)
            newInputTools = set(inputTools.inspect().keys())
            for t in (oldInputTools - newInputTools): diffTools[t] = None
        inputTools.touchReset()
        tools = inputTools.derive()
        inputEnv = inputEnv.derive()
        inputEnv.touchReset()
        inputEnv.setFunArgs({ "recipe" : self, "sandbox" : bool(sandbox) and sandboxEnabled,
            "__tools" : tools })
        env = inputEnv.filter(self.__filterEnv)
        for i in self.__varSelf:
            env = env.derive({ key : env.substitute(value, "environment::"+key)
                               for key, value in i.items() })
        if sandbox is not None:
            name = sandbox.coreStep.corePackage.getName()
            if not checkGlobList(name, self.__filterSandbox):
                sandbox = None
                diffSandbox = None
        states = { n : s.copy() for (n,s) in inputStates.items() }

        # update plugin states
        for s in states.values(): s.onEnter(env, self.__properties)

        # traverse dependencies
        subTreePackages = set()
        directPackages = []
        indirectPackages = []
        provideDeps = UniquePackageList(stack, self.__raiseIncompatibleProvided)
        results = []
        depEnv = env.derive()
        depTools = tools.derive()
        depSandbox = sandbox
        depStates = { n : s.copy() for (n,s) in states.items() }
        depDiffSandbox = diffSandbox
        depDiffTools = diffTools.copy()
        thisDeps = {}
        for dep in self.__deps:
            env.setFunArgs({ "recipe" : self, "sandbox" : bool(sandbox) and sandboxEnabled,
                "__tools" : tools })

            if not env.evaluate(dep.condition, "dependency "+dep.recipe): continue
            r = self.__recipeSet.getRecipe(dep.recipe)
            try:
                if r.__packageName in stack:
                    raise ParseError("Recipes are cyclic (1st package in cylce)")
                depStack = stack + [r.__packageName]
                p, s = r.prepare(depEnv.derive(dep.envOverride),
                                 sandboxEnabled, depStates, depSandbox, depTools,
                                 depStack)
                subTreePackages.add(p.getName())
                subTreePackages.update(s)
                depCoreStep = p.getCorePackageStep()
                depRef = CoreRef(depCoreStep, [p.getName()], depDiffTools, depDiffSandbox)
            except ParseError as e:
                e.pushFrame(r.getPackageName())
                raise e

            # A dependency should be named only once. Hence we can
            # optimistically create the DepTracker object. If the dependency is
            # named more than one we make sure that it is the same variant.
            depTrack = thisDeps.setdefault(dep.recipe, DepTracker(depRef))
            if depTrack.prime():
                directPackages.append(depRef)
            elif depCoreStep.variantId != depTrack.item.refGetDestination().variantId:
                self.__raiseIncompatibleLocal(depCoreStep)
            elif self.__recipeSet.getPolicy('uniqueDependency'):
                raise ParseError("Duplicate dependency '{}'. Each dependency must only be named once!"
                                    .format(dep.recipe))
            else:
                warnDepends.show("{} -> {}".format(self.__packageName, dep.recipe))

            # Remember dependency diffs before changing them
            origDepDiffTools = depDiffTools
            origDepDiffSandbox = depDiffSandbox

            # pick up various results of package
            for (n, s) in states.items():
                if n in dep.use:
                    s.onUse(depCoreStep.corePackage.states[n])
                    if dep.provideGlobal: depStates[n].onUse(depCoreStep.corePackage.states[n])
            if dep.useDeps:
                indirectPackages.extend(
                    CoreRef(d, [p.getName()], origDepDiffTools, origDepDiffSandbox)
                    for d in depCoreStep.providedDeps)
            if dep.useBuildResult and depTrack.useResultOnce():
                results.append(depRef)
            if dep.useTools:
                tools.update(depCoreStep.providedTools)
                diffTools.update( (n, CoreRef(d, [p.getName()], origDepDiffTools, origDepDiffSandbox))
                    for n, d in depCoreStep.providedTools.items() )
                if dep.provideGlobal:
                    depTools.update(depCoreStep.providedTools)
                    depDiffTools = depDiffTools.copy()
                    depDiffTools.update( (n, CoreRef(d, [p.getName()], origDepDiffTools, origDepDiffSandbox))
                        for n, d in depCoreStep.providedTools.items() )
            if dep.useEnv:
                env.update(depCoreStep.providedEnv)
                if dep.provideGlobal: depEnv.update(depCoreStep.providedEnv)
            if dep.useSandbox and (depCoreStep.providedSandbox is not None):
                sandbox = depCoreStep.providedSandbox
                diffSandbox = CoreRef(depCoreStep.providedSandbox, [p.getName()], origDepDiffTools,
                    origDepDiffSandbox)
                if dep.provideGlobal:
                    depSandbox = sandbox
                    depDiffSandbox = diffSandbox
                if sandboxEnabled:
                    env.update(sandbox.environment)
                    if dep.provideGlobal: depEnv.update(sandbox.environment)
            if dep.recipe in self.__provideDeps:
                provideDeps.append(depRef)
                provideDeps.extend(CoreRef(d, [p.getName()], origDepDiffTools, origDepDiffSandbox)
                    for d in depCoreStep.providedDeps)

        # Filter indirect packages and add to result list if necessary. Most
        # likely there are many duplicates that are dropped.
        tmp = indirectPackages
        indirectPackages = []
        for depRef in tmp:
            depCoreStep = depRef.refGetDestination()
            name = depCoreStep.corePackage.getName()
            depTrack = thisDeps.get(name)
            if depTrack is None:
                thisDeps[name] = depTrack = DepTracker(depRef)

            if depTrack.prime():
                indirectPackages.append(depRef)
            elif depCoreStep.variantId != depTrack.item.refGetDestination().variantId:
                self.__raiseIncompatibleProvided(name,
                    stack + depRef.refGetStack(),
                    stack + depTrack.item.refGetStack())

            if depTrack.useResultOnce():
                results.append(depRef)

        # apply tool environments
        toolsEnv = set()
        toolsView = tools.inspect()
        for i in self.__toolDepPackage:
            tool = toolsView.get(i)
            if tool is None: continue
            if not tool.environment: continue
            tmp = set(tool.environment.keys())
            if not tmp.isdisjoint(toolsEnv):
                self.__raiseIncompatibleTools(toolsView)
            toolsEnv.update(tmp)
            env.update(tool.environment)

        # apply private environment
        env.setFunArgs({ "recipe" : self, "sandbox" : bool(sandbox) and sandboxEnabled,
            "__tools" : tools })
        for i in self.__varPrivate:
            env = env.derive({ key : env.substitute(value, "privateEnvironment::"+key)
                               for key, value in i.items() })

        # meta variables override existing variables but can not be substituted
        env.update(self.__metaEnv)

        # set fixed built-in variables
        env['BOB_RECIPE_NAME'] = self.__baseName
        env['BOB_PACKAGE_NAME'] = self.__packageName

        # record used environment and tools
        env.touch(self.__packageVars | self.__packageVarsWeak)
        tools.touch(self.__toolDepPackage)

        # Check if fingerprinting has to be applied. At least one
        # 'fingerprintIf' must evaluate to 'True'. The mask of included
        # fingerprints is stored in the package instead of the final string to
        # save memory.
        doFingerprint = 0
        doFingerprintMaybe = 0
        mask = 1
        fingerprintConditions = chain(
            (t.fingerprintIf for t in (toolsView.get(i) for i in sorted(self.__toolDepPackage))
                             if t is not None),
            self.__fingerprintIf)
        for fingerprintIf in fingerprintConditions:
            if fingerprintIf is None:
                doFingerprintMaybe |= mask
            elif fingerprintIf == True:
                doFingerprint |= mask
            elif isinstance(fingerprintIf, str) and env.evaluate(fingerprintIf, "fingerprintIf"):
                doFingerprint |= mask
            mask <<= 1
        if doFingerprint:
            doFingerprint |= doFingerprintMaybe

        # create package
        # touchedTools = tools.touchedKeys()
        # diffTools = { n : t for n,t in diffTools.items() if n in touchedTools }
        p = CorePackage(self, tools.detach(), diffTools, sandbox, diffSandbox,
                directPackages, indirectPackages, states, uidGen(), doFingerprint)

        # optional checkout step
        if self.__checkout != (None, None, [], []):
            checkoutDigestEnv = env.prune(self.__checkoutVars)
            checkoutEnv = ( env.prune(self.__checkoutVars | self.__checkoutVarsWeak)
                if self.__checkoutVarsWeak else checkoutDigestEnv )
            srcCoreStep = p.createCoreCheckoutStep(self.__checkout, env, checkoutDigestEnv, checkoutEnv)
        else:
            srcCoreStep = p.createInvalidCoreCheckoutStep()

        # optional build step
        if self.__build != (None, None):
            buildDigestEnv = env.prune(self.__buildVars)
            buildEnv = ( env.prune(self.__buildVars | self.__buildVarsWeak)
                if self.__buildVarsWeak else buildDigestEnv )
            buildCoreStep = p.createCoreBuildStep(self.__build, buildDigestEnv, buildEnv,
                [CoreRef(srcCoreStep)] + results)
        else:
            buildCoreStep = p.createInvalidCoreBuildStep()

        # mandatory package step
        packageDigestEnv = env.prune(self.__packageVars)
        packageEnv = ( env.prune(self.__packageVars | self.__packageVarsWeak)
            if self.__packageVarsWeak else packageDigestEnv )
        packageCoreStep = p.createCorePackageStep(self.__package, packageDigestEnv, packageEnv,
            [CoreRef(buildCoreStep)])

        # provide environment
        provideEnv = {}
        for (key, value) in self.__provideVars.items():
            provideEnv[key] = env.substitute(value, "provideVars::"+key)
        packageCoreStep.providedEnv = provideEnv

        # provide tools
        packageCoreStep.providedTools = { name : tool.prepare(packageCoreStep, env)
            for (name, tool) in self.__provideTools.items() }

        # provide deps (direct and indirect deps)
        packageCoreStep.providedDeps = provideDeps.result()

        # provide Sandbox
        if self.__provideSandbox:
            packageCoreStep.providedSandbox = CoreSandbox(packageCoreStep,
                env, sandboxEnabled, self.__provideSandbox)

        # update plugin states
        for s in states.values(): s.onFinish(env, self.__properties)

        if self.__shared:
            if not packageCoreStep.isDeterministic():
                raise ParseError("Shared packages must be deterministic!")

        # remember calculated package
        if reusedCorePackage is None:
            pid = packageCoreStep.getResultId()
            reusableCorePackage = self.__corePackagesById.setdefault(pid, p)
            if reusableCorePackage is not p:
                p = reusableCorePackage
            self.__corePackagesByMatch.insert(0, PackageMatcher(
                reusableCorePackage, inputEnv, inputTools, inputStates,
                inputSandbox, subTreePackages))
        elif packageCoreStep.getResultId() != reusedCorePackage.getCorePackageStep().getResultId():
            raise AssertionError("Wrong reusage for " + "/".join(stack))
        else:
            # drop calculated package to keep memory consumption low
            p = reusedCorePackage

        return p, subTreePackages

    def _getBuildNetAccess(self):
        if self.__buildNetAccess is None:
            return not self.__recipeSet.getPolicy("offlineBuild")
        else:
            return self.__buildNetAccess

    def _getPackageNetAccess(self):
        if self.__packageNetAccess is None:
            return not self.__recipeSet.getPolicy("offlineBuild")
        else:
            return self.__packageNetAccess

    def __raiseIncompatibleProvided(self, name, stack1, stack2):
        raise ParseError("Incompatible variants of package: {} vs. {}"
            .format("/".join(stack1), "/".join(stack2)),
            help=
"""This error is caused by '{PKG}' that is passed upwards via 'provideDeps' from multiple dependencies of '{CUR}'.
These dependencies constitute different variants of '{PKG}' and can therefore not be used in '{CUR}'."""
    .format(PKG=name, CUR=self.__packageName))

    def __raiseIncompatibleLocal(self, r):
        raise ParseError("Multiple incompatible dependencies to package: {}"
            .format(r.corePackage.getName()),
            help=
"""This error is caused by naming '{PKG}' multiple times in the recipe with incompatible variants.
Every dependency must only be given once."""
    .format(PKG=r.corePackage.getName(), CUR=self.__packageName))

    def __raiseIncompatibleTools(self, tools):
        toolsVars = {}
        for i in self.__toolDepPackage:
            tool = tools.get(i)
            if tool is None: continue
            for k in tool.environment.keys():
                toolsVars.setdefault(k, []).append(i)
        toolsVars = ", ".join(sorted(
            "'{}' defined by {}".format(k, " and ".join(sorted(v)))
            for k,v in toolsVars.items() if len(v) > 1))
        raise ParseError("Multiple tools defined the same environment variable(s): {}"
            .format(toolsVars),
            help="Each environment variable must be defined only by one used tool.")

    @property
    def checkoutScript(self):
        return self.__checkout[0] or ""

    @property
    def checkoutDigestScript(self):
        return self.__checkout[1] or ""

    @property
    def checkoutDeterministic(self):
        return self.__checkoutDeterministic

    @property
    def checkoutAsserts(self):
        return [ CheckoutAssert(cassert) for cassert in self.__checkout[3] ]

    @property
    def buildScript(self):
        return self.__build[0]

    @property
    def buildDigestScript(self):
        return self.__build[1]

    @property
    def packageScript(self):
        return self.__package[0]

    @property
    def packageDigestScript(self):
        return self.__package[1]

    @property
    def toolDepCheckout(self):
        return self.__toolDepCheckout

    @property
    def toolDepBuild(self):
        return self.__toolDepBuild

    @property
    def toolDepPackage(self):
        return self.__toolDepPackage

    @property
    def fingerprintScripts(self):
        return self.__fingerprintScripts


class PackageMatcher:
    def __init__(self, corePackage, env, tools, states, sandbox, subTreePackages):
        self.corePackage = corePackage
        envData = env.inspect()
        self.env = { name : envData.get(name) for name in env.touchedKeys() }
        toolsData = tools.inspect()
        self.tools = { name : (tool.coreStep.variantId if tool is not None else None)
            for (name, tool) in ( (n, toolsData.get(n)) for n in tools.touchedKeys() ) }
        self.states = { n : s.copy() for (n,s) in states.items() }
        self.sandbox = sandbox.coreStep.variantId if sandbox is not None else None
        self.subTreePackages = subTreePackages

    def matches(self, inputEnv, inputTools, inputStates, inputSandbox):
        for (name, env) in self.env.items():
            if env != inputEnv.get(name): return False
        for (name, tool) in self.tools.items():
            match = inputTools.get(name)
            match = match.coreStep.variantId if match is not None else None
            if tool != match: return False
        match = inputSandbox.coreStep.variantId \
            if inputSandbox is not None else None
        if self.sandbox != match: return False
        if self.states != inputStates: return False
        return True

    def touch(self, inputEnv, inputTools):
        inputEnv.touch(self.env.keys())
        inputTools.touch(self.tools.keys())


class ArchiveValidator:
    def __init__(self):
        self.__validTypes = schema.Schema({'backend': schema.Or('none', 'file', 'http', 'shell', 'azure')},
            ignore_extra_keys=True)
        baseArchive = {
            'backend' : str,
            schema.Optional('flags') : schema.Schema(["download", "upload",
                "nofail", "nolocal", "nojenkins"])
        }
        fileArchive = baseArchive.copy()
        fileArchive["path"] = str
        httpArchive = baseArchive.copy()
        httpArchive["url"] = str
        httpArchive[schema.Optional("sslVerify")] = bool
        shellArchive = baseArchive.copy()
        shellArchive.update({
            schema.Optional('download') : str,
            schema.Optional('upload') : str,
        })
        azureArchive = baseArchive.copy()
        azureArchive.update({
            'account' : str,
            'container' : str,
            schema.Optional('key') : str,
            schema.Optional('sasToken"') : str,
        })
        self.__backends = {
            'none' : schema.Schema(baseArchive),
            'file' : schema.Schema(fileArchive),
            'http' : schema.Schema(httpArchive),
            'shell' : schema.Schema(shellArchive),
            'azure' : schema.Schema(azureArchive),
        }

    def validate(self, data):
        self.__validTypes.validate(data)
        return self.__backends[data['backend']].validate(data)

class MountValidator:
    def __init__(self):
        self.__options = schema.Schema(
            ["nolocal", "nojenkins", "nofail", "rw"],
            error="Invalid mount option specified!")

    def validate(self, data):
        if isinstance(data, str):
            return (data, data, [])
        elif isinstance(data, list) and (len(data) in [2, 3]):
            if not isinstance(data[0], str):
                raise schema.SchemaError(None, "Expected string as first mount argument!")
            if not isinstance(data[1], str):
                raise schema.SchemaError(None, "Expected string as second mount argument!")
            if len(data) == 3:
                self.__options.validate(data[2])
                return tuple(data)
            else:
                return (data[0], data[1], [])

        raise schema.SchemaError(None, "Mount entry must be a string or a two/three items list!")

class RecipeSet:

    BUILD_DEV_SCHEMA = schema.Schema(
        {
            schema.Optional('destination') : str,
            schema.Optional('force') : bool,
            schema.Optional('no_deps') : bool,
            schema.Optional('build_mode') : schema.Or("build-only","normal", "checkout-only"),
            schema.Optional('checkout_only') : bool,
            schema.Optional('clean') : bool,
            schema.Optional('verbosity') : int,
            schema.Optional('no_logfiles') : bool,
            schema.Optional('link_deps') : bool,
            schema.Optional('upload') : bool,
            schema.Optional('download') : schema.Or("yes", "no", "deps", "forced", "forced-deps"),
            schema.Optional('sandbox') : bool,
            schema.Optional('clean_checkout') : bool,
            schema.Optional('always_checkout') : [str],
            schema.Optional('jobs') : int,
        })

    GRAPH_SCHEMA = schema.Schema(
        {
            schema.Optional('options') : schema.Schema({str : schema.Or(str, bool)}),
            schema.Optional('type') : schema.Or("d3", "dot"),
            schema.Optional('max_depth') : int,
        })

    STATIC_CONFIG_SCHEMA = schema.Schema({
        schema.Optional('bobMinimumVersion') : schema.Regex(r'^[0-9]+(\.[0-9]+){0,2}$'),
        schema.Optional('plugins') : [str],
        schema.Optional('policies') : schema.Schema(
            {
                schema.Optional('relativeIncludes') : bool,
                schema.Optional('cleanEnvironment') : bool,
                schema.Optional('tidyUrlScm') : bool,
                schema.Optional('allRelocatable') : bool,
                schema.Optional('offlineBuild') : bool,
                schema.Optional('sandboxInvariant') : bool,
                schema.Optional('uniqueDependency') : bool,
                schema.Optional('mergeEnvironment') : bool,
                schema.Optional('secureSSL') : bool,
            },
            error="Invalid policy specified! Maybe your Bob is too old?"
        ),
        schema.Optional('layers') : [str],
    })

    _ignoreCmdConfig = False
    @classmethod
    def ignoreCommandCfg(cls):
        cls._ignoreCmdConfig = True

    _colorModeConfig = None
    @classmethod
    def setColorModeCfg(cls, mode):
        cls._colorModeConfig = mode

    def __init__(self):
        self.__defaultEnv = {}
        self.__aliases = {}
        self.__recipes = {}
        self.__classes = {}
        self.__whiteList = set(["TERM", "SHELL", "USER", "HOME"])
        self.__archive = { "backend" : "none" }
        self.__rootFilter = []
        self.__scmOverrides = []
        self.__hooks = {}
        self.__projectGenerators = {}
        self.__configFiles = []
        self.__properties = {}
        self.__states = {}
        self.__cache = YamlCache()
        self.__stringFunctions = DEFAULT_STRING_FUNS.copy()
        self.__plugins = {}
        self.__commandConfig = {}
        self.__uiConfig = {}
        self.__policies = {
            'relativeIncludes' : (
                "0.13",
                InfoOnce("relativeIncludes policy not set. Using project root directory as base for all includes!",
                    help="See http://bob-build-tool.readthedocs.io/en/latest/manual/policies.html#relativeincludes for more information.")
            ),
            'cleanEnvironment' : (
                "0.13",
                InfoOnce("cleanEnvironment policy not set. Initial environment tainted by whitelisted variables!",
                    help="See http://bob-build-tool.readthedocs.io/en/latest/manual/policies.html#cleanenvironment for more information.")
            ),
            'tidyUrlScm' : (
                "0.14",
                InfoOnce("tidyUrlScm policy not set. Updating URL SCMs in develop build mode is not entirely safe!",
                    help="See http://bob-build-tool.readthedocs.io/en/latest/manual/policies.html#tidyurlscm for more information.")
            ),
            'allRelocatable' : (
                "0.14",
                InfoOnce("allRelocatable policy not set. Packages that define tools are not up- or downloaded.",
                    help="See http://bob-build-tool.readthedocs.io/en/latest/manual/policies.html#allrelocatable for more information.")
            ),
            'offlineBuild' : (
                "0.14",
                InfoOnce("offlineBuild policy not set. Network access still allowed during build steps.",
                    help="See http://bob-build-tool.readthedocs.io/en/latest/manual/policies.html#offlinebuild for more information.")
            ),
            'sandboxInvariant' : (
                "0.14",
                InfoOnce("sandboxInvariant policy not set. Inconsistent sandbox handling for binary artifacts.",
                    help="See http://bob-build-tool.readthedocs.io/en/latest/manual/policies.html#sandboxinvariant for more information.")
            ),
            'uniqueDependency' : (
                "0.14",
                InfoOnce("uniqueDependency policy not set. Naming same dependency multiple times is deprecated.",
                    help="See http://bob-build-tool.readthedocs.io/en/latest/manual/policies.html#uniquedependency for more information.")
            ),
            'mergeEnvironment' : (
                "0.15",
                InfoOnce("mergeEnvironment policy not set. Recipe and classes (private)environments overwrite each other instead of being merged.",
                    help="See http://bob-build-tool.readthedocs.io/en/latest/manual/policies.html#mergeenvironment for more information.")
            ),
            'secureSSL' : (
                "0.15",
                InfoOnce("secureSSL policy not set. Bob will ignore SSL certificate errors.",
                    help="See http://bob-build-tool.readthedocs.io/en/latest/manual/policies.html#securessl for more information.")
            ),
        }
        self.__buildHooks = {}
        self.__sandboxOpts = {}

        def updateArchive(x): self.__archive = x

        self.__settings = {
            "alias" : BuiltinSetting(
                schema.Schema({ schema.Regex(r'^[0-9A-Za-z_-]+$') : str }),
                lambda x: self.__aliases.update(x)
            ),
            "archive" : BuiltinSetting(
                schema.Or(
                    ArchiveValidator(),
                    schema.Schema( [ArchiveValidator()] )
                ),
                updateArchive
            ),
            "command" : BuiltinSetting(
                schema.Schema({
                    schema.Optional('dev') : self.BUILD_DEV_SCHEMA,
                    schema.Optional('build') : self.BUILD_DEV_SCHEMA,
                    schema.Optional('graph') : self.GRAPH_SCHEMA
                }),
                lambda x: updateDicRecursive(self.__commandConfig, x) if not self._ignoreCmdConfig else None
            ),
            "environment" : BuiltinSetting(
                schema.Schema({ schema.Regex(r'^[A-Za-z_][A-Za-z0-9_]*$') : str }),
                lambda x: self.__defaultEnv.update(x)
            ),
            "hooks" : BuiltinSetting(
                schema.Schema({
                    schema.Optional('preBuildHook') : str,
                    schema.Optional('postBuildHook') : str,
                }),
                lambda x: self.__buildHooks.update(x)
            ),
            "rootFilter" : BuiltinSetting(
                schema.Schema([str]),
                lambda x: self.__rootFilter.extend(x)
            ),
            "sandbox" : BuiltinSetting(
                schema.Schema({
                    schema.Optional('mount') : schema.Schema([ MountValidator() ]),
                    schema.Optional('paths') : [str],
                }),
                lambda x: updateDicRecursive(self.__sandboxOpts, x),
                True
            ),
            "scmOverrides" : BuiltinSetting(
                schema.Schema([{
                    schema.Optional('match') : schema.Schema({ str: str }),
                    schema.Optional('del') : [
                        "branch", "commit", "digestSHA1", "digestSHA256", "dir",
                        "extract", "fileName", "if", "rev", "revision", "tag"
                    ],
                    schema.Optional('set') : schema.Schema({ str : str }),
                    schema.Optional('replace') : schema.Schema({
                        str : schema.Schema({
                            'pattern' : str,
                            'replacement' : str
                        })
                    })
                }]),
                lambda x: self.__scmOverrides.extend([ ScmOverride(o) for o in x ])
            ),
            "ui" : BuiltinSetting(
                schema.Schema({
                    schema.Optional('color') : schema.Or('never', 'always', 'auto')
                }),
                lambda x: updateDicRecursive(self.__uiConfig, x)
            ),
            "whitelist" : BuiltinSetting(
                schema.Schema([ schema.Regex(r'^[A-Za-z_][A-Za-z0-9_]*$') ]),
                lambda x: self.__whiteList.update(x)
            ),
        }

    def __addRecipe(self, recipe):
        name = recipe.getPackageName()
        if name in self.__recipes:
            raise ParseError("Package "+name+" already defined")
        self.__recipes[name] = recipe

    def __addClass(self, recipe):
        name = recipe.getPackageName()
        if name in self.__classes:
            raise ParseError("Class "+name+" already defined")
        self.__classes[name] = recipe

    def __loadPlugins(self, rootDir, layer, plugins):
        for p in plugins:
            name = os.path.join(rootDir, "plugins", p+".py")
            if not os.path.exists(name):
                raise ParseError("Plugin '"+name+"' not found!")
            mangledName = "__bob_plugin_" + "".join("layers_"+l+"_" for l in layer) + p
            self.__plugins[mangledName] = self.__loadPlugin(mangledName, name, p)

    def __loadPlugin(self, mangledName, fileName, name):
        # dummy load file to hash state
        self.loadBinary(fileName)
        try:
            from importlib.machinery import SourceFileLoader
            loader = SourceFileLoader(mangledName, fileName)
            mod = loader.load_module()
        except SyntaxError as e:
            import traceback
            raise ParseError("Error loading plugin "+fileName+": "+str(e),
                             help=traceback.format_exc())
        except Exception as e:
            raise ParseError("Error loading plugin "+fileName+": "+str(e))

        try:
            manifest = mod.manifest
        except AttributeError:
            raise ParseError("Plugin '"+fileName+"' did not define 'manifest'!")
        apiVersion = manifest.get('apiVersion')
        if apiVersion is None:
            raise ParseError("Plugin '"+fileName+"' did not define 'apiVersion'!")
        if compareVersion(BOB_VERSION, apiVersion) < 0:
            raise ParseError("Your Bob is too old. Plugin '"+fileName+"' requires at least version "+apiVersion+"!")
        toolsAbiBreak = compareVersion(apiVersion, "0.15") < 0

        hooks = manifest.get('hooks', {})
        if not isinstance(hooks, dict):
            raise ParseError("Plugin '"+fileName+"': 'hooks' has wrong type!")
        for (hook, fun) in hooks.items():
            if not isinstance(hook, str):
                raise ParseError("Plugin '"+fileName+"': hook name must be a string!")
            if not callable(fun):
                raise ParseError("Plugin '"+fileName+"': "+hook+": hook must be callable!")
            self.__hooks.setdefault(hook, []).append(fun)

        projectGenerators = manifest.get('projectGenerators', {})
        if not isinstance(projectGenerators, dict):
            raise ParseError("Plugin '"+fileName+"': 'projectGenerators' has wrong type!")
        self.__projectGenerators.update(projectGenerators)

        properties = manifest.get('properties', {})
        if not isinstance(properties, dict):
            raise ParseError("Plugin '"+fileName+"': 'properties' has wrong type!")
        for (i,j) in properties.items():
            if not isinstance(i, str):
                raise ParseError("Plugin '"+fileName+"': property name must be a string!")
            if not issubclass(j, PluginProperty):
                raise ParseError("Plugin '"+fileName+"': property '" +i+"' has wrong type!")
            if i in self.__properties:
                raise ParseError("Plugin '"+fileName+"': property '" +i+"' already defined by other plugin!")
        self.__properties.update(properties)

        states = manifest.get('state', {})
        if not isinstance(states, dict):
            raise ParseError("Plugin '"+fileName+"': 'states' has wrong type!")
        for (i,j) in states.items():
            if not isinstance(i, str):
                raise ParseError("Plugin '"+fileName+"': state tracker name must be a string!")
            if i in ["environment", "tools", "result", "deps", "sandbox"]:
                raise ParseError("Plugin '"+fileName+"': state tracker has reserved name!")
            if not issubclass(j, PluginState):
                raise ParseError("Plugin '"+fileName+"': state tracker '" +i+"' has wrong type!")
            if i in self.__states:
                raise ParseError("Plugin '"+fileName+"': state tracker '" +i+"' already defined by other plugin!")
        if states and toolsAbiBreak:
            warnDeprecatedPluginState.show(fileName)
            for i in states.values(): pluginStateCompat(i)
        self.__states.update(states)

        funs = manifest.get('stringFunctions', {})
        if not isinstance(funs, dict):
            raise ParseError("Plugin '"+fileName+"': 'stringFunctions' has wrong type!")
        for (i,j) in funs.items():
            if not isinstance(i, str):
                raise ParseError("Plugin '"+fileName+"': string function name must be a string!")
            if i in self.__stringFunctions:
                raise ParseError("Plugin '"+fileName+"': string function '" +i+"' already defined by other plugin!")
        if funs and toolsAbiBreak:
            warnDeprecatedStringFn.show(fileName)
            funs = { i : pluginStringFunCompat(j) for i, j in funs.items() }
        self.__stringFunctions.update(funs)

        settings = manifest.get('settings', {})
        if not isinstance(settings, dict):
            raise ParseError("Plugin '"+fileName+"': 'settings' has wrong type!")
        for (i,j) in settings.items():
            if not isinstance(i, str):
                raise ParseError("Plugin '"+fileName+"': settings name must be a string!")
            if i[:1].islower():
                raise ParseError("Plugin '"+fileName+"': settings name must not start lower case!")
            if not isinstance(j, PluginSetting):
                raise ParseError("Plugin '"+fileName+"': setting '"+i+"' has wrong type!")
            if i in self.__settings:
                raise ParseError("Plugin '"+fileName+"': setting '"+i+"' already defined by other plugin!")
        self.__settings.update(settings)

        return mod

    def defineHook(self, name, value):
        self.__hooks[name] = [value]

    def setConfigFiles(self, configFiles):
        self.__configFiles = configFiles

    def getCommandConfig(self):
        return self.__commandConfig

    def getHook(self, name):
        return self.__hooks[name][-1]

    def getHookStack(self, name):
        return self.__hooks.get(name, [])

    def getProjectGenerators(self):
        return self.__projectGenerators

    def envWhiteList(self):
        return set(self.__whiteList)

    def archiveSpec(self):
        return self.__archive

    def defaultEnv(self):
        return self.__defaultEnv

    def scmOverrides(self):
        return self.__scmOverrides

    def getScmAudit(self):
        try:
            ret = self.__recipeScmAudit
        except AttributeError:
            ret = self.__recipeScmAudit = auditFromDir(".")
        return ret

    def getScmStatus(self):
        audit = self.getScmAudit()
        if audit is None:
            return "unknown"
        else:
            return audit.getStatusLine()

    def getBuildHook(self, name):
        return self.__buildHooks.get(name)

    def getSandboxMounts(self):
        return self.__sandboxOpts.get("mount", [])

    def getSandboxPaths(self):
        return list(reversed(self.__sandboxOpts.get("paths", [])))

    def loadBinary(self, path):
        return self.__cache.loadBinary(path)

    def loadYaml(self, path, schema, default={}):
        if os.path.exists(path):
            return self.__cache.loadYaml(path, schema, default)
        else:
            return default

    def parse(self):
        if not os.path.isdir("recipes"):
            raise ParseError("No recipes directory found.")
        self.__cache.open()
        try:
            self.__parse()

            # config files overrule everything else
            for c in self.__configFiles:
                c = str(c) + ".yaml"
                if not os.path.isfile(c):
                    raise ParseError("Config file {} does not exist!".format(c))
                self.__parseUserConfig(c)
        finally:
            self.__cache.close()

    def __parse(self):
        # Begin with root layer
        self.__parseLayer([], "9999")

        # resolve recipes and their classes
        rootRecipes = []
        for recipe in self.__recipes.values():
            try:
                recipe.resolveClasses()
            except ParseError as e:
                e.pushFrame(recipe.getPackageName())
                raise
            if recipe.isRoot():
                rootRecipes.append(recipe.getPackageName())

        filteredRoots = [ root for root in rootRecipes
                if (len(self.__rootFilter) == 0) or checkGlobList(root, maybeGlob(self.__rootFilter)) ]
        # create virtual root package
        self.__rootRecipe = Recipe.createVirtualRoot(self, sorted(filteredRoots), self.__properties)
        self.__addRecipe(self.__rootRecipe)

    def __parseLayer(self, layer, maxVer):
        rootDir = os.path.join("", *(os.path.join("layers", l) for l in layer))
        if not os.path.isdir(rootDir or "."):
            raise ParseError("Layer '{}' does not exist!".format("/".join(layer)))

        config = self.loadYaml(os.path.join(rootDir, "config.yaml"), RecipeSet.STATIC_CONFIG_SCHEMA)
        minVer = config.get("bobMinimumVersion", "0.1")
        if compareVersion(BOB_VERSION, minVer) < 0:
            raise ParseError("Your Bob is too old. At least version "+minVer+" is required!")
        if compareVersion(maxVer, minVer) < 0:
            raise ParseError("Layer '{}' reqires a higher Bob version than root project!"
                                .format("/".join(layer)))
        maxVer = minVer # sub-layers must not have a higher bobMinimumVersion
        self.__loadPlugins(rootDir, layer, config.get("plugins", []))
        self.__createSchemas()

        # Determine policies. The root layer determines the default settings
        # implicitly by bobMinimumVersion or explicitly via 'policies'. All
        # sub-layer policies must not contradict root layer policies
        if layer:
            for (name, behaviour) in config.get("policies", {}).items():
                if bool(self.__policies[name][0]) != behaviour:
                    raise ParseError("Layer '{}' requires different behaviour for policy '{}' than root project!"
                                        .format("/".join(layer), name))
        else:
            self.__policies = { name : (True if compareVersion(ver, minVer) <= 0 else None, warn)
                for (name, (ver, warn)) in self.__policies.items() }
            for (name, behaviour) in config.get("policies", {}).items():
                self.__policies[name] = (behaviour, None)

        # global user config(s)
        if not DEBUG['ngd'] and not layer:
            self.__parseUserConfig("/etc/bobdefault.yaml", True)
            self.__parseUserConfig(os.path.join(os.environ.get('XDG_CONFIG_HOME',
                os.path.join(os.path.expanduser("~"), '.config')), 'bob', 'default.yaml'), True)

        # First parse any sub-layers. Their settings have a lower precedence
        # and may be overwritten by higher layers.
        for l in config.get("layers", []):
            self.__parseLayer(layer + [l], maxVer)

        # project user config(s)
        if layer and not self.getPolicy("relativeIncludes"):
            raise ParseError("Layers require the relativeIncludes policy to be set to the new behaviour!")
        self.__parseUserConfig(os.path.join(rootDir, "default.yaml"))

        # color mode provided in cmd line takes precedence
        # (if no color mode provided by user, default one will be used)
        setColorMode(self._colorModeConfig or self.__uiConfig.get('color', 'auto'))

        # finally parse recipes
        classesDir = os.path.join(rootDir, 'classes')
        for root, dirnames, filenames in os.walk(classesDir):
            for path in fnmatch.filter(filenames, "*.yaml"):
                try:
                    [r] = Recipe.loadFromFile(self, layer, classesDir,
                        os.path.relpath(os.path.join(root, path), classesDir),
                        self.__properties, self.__classSchema, False)
                    self.__addClass(r)
                except ParseError as e:
                    e.pushFrame(path)
                    raise

        recipesDir = os.path.join(rootDir, 'recipes')
        for root, dirnames, filenames in os.walk(recipesDir):
            for path in fnmatch.filter(filenames, "*.yaml"):
                try:
                    recipes = Recipe.loadFromFile(self, layer, recipesDir,
                        os.path.relpath(os.path.join(root, path), recipesDir),
                        self.__properties, self.__recipeSchema, True)
                    for r in recipes:
                        self.__addRecipe(r)
                except ParseError as e:
                    e.pushFrame(path)
                    raise

    def __parseUserConfig(self, fileName, relativeIncludes=None):
        if relativeIncludes is None:
            relativeIncludes = self.getPolicy("relativeIncludes")
        cfg = self.loadYaml(fileName, self.__userConfigSchema)
        for (name, value) in cfg.items():
            if name != "include" and name != "require": self.__settings[name].merge(value)
        for p in cfg.get("require", []):
            p = (os.path.join(os.path.dirname(fileName), p) if relativeIncludes else p) + ".yaml"
            if not os.path.isfile(p):
                raise ParseError("Include file '{}' (required by '{}') does not exist!"
                                    .format(p, fileName))
            self.__parseUserConfig(p, relativeIncludes)
        for p in cfg.get("include", []):
            p = os.path.join(os.path.dirname(fileName), p) if relativeIncludes else p
            self.__parseUserConfig(p + ".yaml", relativeIncludes)

    def __createSchemas(self):
        varNameUseSchema = schema.Regex(r'^[A-Za-z_][A-Za-z0-9_]*$')
        varFilterSchema = schema.Regex(r'^!?[][A-Za-z_*?][][A-Za-z0-9_*?]*$')
        recipeFilterSchema = schema.Regex(r'^!?[][0-9A-Za-z_.+:*?-]+$')
        toolNameSchema = schema.Regex(r'^[0-9A-Za-z_.+:-]+$')

        useClauses = ['deps', 'environment', 'result', 'tools', 'sandbox']
        useClauses.extend(self.__states.keys())

        # construct recursive depends clause
        dependsInnerClause = {
            schema.Optional('name') : str,
            schema.Optional('use') : useClauses,
            schema.Optional('forward') : bool,
            schema.Optional('environment') : VarDefineValidator("depends::environment"),
            schema.Optional('if') : str
        }
        dependsClause = schema.Schema([
            schema.Or(
                str,
                schema.Schema(dependsInnerClause)
            )
        ])
        dependsInnerClause[schema.Optional('depends')] = dependsClause

        classSchemaSpec = {
            schema.Optional('checkoutScript') : str,
            schema.Optional('buildScript') : str,
            schema.Optional('packageScript') : str,
            schema.Optional('checkoutTools') : [ toolNameSchema ],
            schema.Optional('buildTools') : [ toolNameSchema ],
            schema.Optional('packageTools') : [ toolNameSchema ],
            schema.Optional('checkoutVars') : [ varNameUseSchema ],
            schema.Optional('buildVars') : [ varNameUseSchema ],
            schema.Optional('packageVars') : [ varNameUseSchema ],
            schema.Optional('checkoutVarsWeak') : [ varNameUseSchema ],
            schema.Optional('buildVarsWeak') : [ varNameUseSchema ],
            schema.Optional('packageVarsWeak') : [ varNameUseSchema ],
            schema.Optional('checkoutDeterministic') : bool,
            schema.Optional('checkoutSCM') : ScmValidator({
                'git' : GitScm.SCHEMA,
                'svn' : SvnScm.SCHEMA,
                'cvs' : CvsScm.SCHEMA,
                'url' : UrlScm.SCHEMA
            }),
            schema.Optional('checkoutAssert') : [ CheckoutAssert.SCHEMA ],
            schema.Optional('depends') : dependsClause,
            schema.Optional('environment') : VarDefineValidator("environment"),
            schema.Optional('filter') : schema.Schema({
                schema.Optional('environment') : [ varFilterSchema ],
                schema.Optional('tools') : [ recipeFilterSchema ],
                schema.Optional('sandbox') : [ recipeFilterSchema ]
            }),
            schema.Optional('inherit') : [str],
            schema.Optional('privateEnvironment') : VarDefineValidator("privateEnvironment"),
            schema.Optional('metaEnvironment') : VarDefineValidator("metaEnvironment"),
            schema.Optional('provideDeps') : [str],
            schema.Optional('provideTools') : schema.Schema({
                str: schema.Or(
                    str,
                    schema.Schema({
                        'path' : str,
                        schema.Optional('libs') : [str],
                        schema.Optional('netAccess') : bool,
                        schema.Optional('environment') : VarDefineValidator("provideTools::environment"),
                        schema.Optional('fingerprintScript') : str,
                        schema.Optional('fingerprintIf') : schema.Or(None, str, bool),
                    })
                )
            }),
            schema.Optional('provideVars') : VarDefineValidator("provideVars"),
            schema.Optional('provideSandbox') : schema.Schema({
                'paths' : [str],
                schema.Optional('mount') : schema.Schema([ MountValidator() ],
                    error="provideSandbox: invalid 'mount' property"),
                schema.Optional('environment') : VarDefineValidator("provideSandbox::environment"),
            }),
            schema.Optional('root') : bool,
            schema.Optional('shared') : bool,
            schema.Optional('relocatable') : bool,
            schema.Optional('buildNetAccess') : bool,
            schema.Optional('packageNetAccess') : bool,
            schema.Optional('fingerprintScript') : str,
            schema.Optional('fingerprintIf') : schema.Or(None, str, bool),
        }
        for (name, prop) in self.__properties.items():
            classSchemaSpec[schema.Optional(name)] = schema.Schema(prop.validate,
                error="property '"+name+"' has an invalid type")

        self.__classSchema = schema.Schema(classSchemaSpec)

        recipeSchemaSpec = classSchemaSpec.copy()
        recipeSchemaSpec[schema.Optional('multiPackage')] = schema.Schema({
            MULTIPACKAGE_NAME_SCHEMA : recipeSchemaSpec
        })
        self.__recipeSchema = schema.Schema(recipeSchemaSpec)

        userConfigSchemaSpec = {
            schema.Optional('include') : schema.Schema([str]),
            schema.Optional('require') : schema.Schema([str]),
        }
        for (name, setting) in self.__settings.items():
            userConfigSchemaSpec[schema.Optional(name)] = schema.Schema(setting.validate,
                error="setting '"+name+"' has an invalid type")
        self.__userConfigSchema = schema.Schema(userConfigSchemaSpec)


    def getRecipe(self, packageName):
        if packageName not in self.__recipes:
            raise ParseError("Package {} requested but not found.".format(packageName))
        return self.__recipes[packageName]

    def getClass(self, className):
        if className not in self.__classes:
            raise ParseError("Class {} requested but not found.".format(className))
        return self.__classes[className]

    def __getEnvWithCacheKey(self, envOverrides, sandboxEnabled):
        # calculate start environment
        if self.getPolicy("cleanEnvironment"):
            osEnv = Env(os.environ)
            osEnv.setFuns(self.__stringFunctions)
            env = Env({ k : osEnv.substitute(v, k) for (k, v) in
                self.__defaultEnv.items() })
        else:
            env = Env(os.environ).prune(self.__whiteList)
            env.update(self.__defaultEnv)
        env.setFuns(self.__stringFunctions)
        env.update(envOverrides)

        # calculate cache key for persisted packages
        h = hashlib.sha1()
        h.update(BOB_INPUT_HASH)
        h.update(self.__cache.getDigest())
        h.update(struct.pack("<I", len(env)))
        for (key, val) in sorted(env.inspect().items()):
            h.update(struct.pack("<II", len(key), len(val)))
            h.update((key+val).encode('utf8'))
        h.update(b'\x01' if sandboxEnabled else b'\x00')
        return (env, h.digest())

    def __generatePackages(self, nameFormatter, env, cacheKey, sandboxEnabled):
        # use separate caches with and without sandbox
        if sandboxEnabled:
            cacheName = ".bob-packages-sb.pickle"
        else:
            cacheName = ".bob-packages.pickle"

        # try to load the persisted packages
        try:
            with open(cacheName, "rb") as f:
                persistedCacheKey = f.read(len(cacheKey))
                if cacheKey == persistedCacheKey:
                    tmp = PackageUnpickler(f, self.getRecipe, self.__plugins,
                                           nameFormatter).load()
                    return tmp.refDeref([], {}, None, nameFormatter)
        except (EOFError, OSError, pickle.UnpicklingError):
            pass

        # not cached -> calculate packages
        states = { n:s() for (n,s) in self.__states.items() }
        result = self.__rootRecipe.prepare(env, sandboxEnabled, states)[0]

        # save package tree for next invocation
        try:
            newCacheName = cacheName + ".new"
            with open(newCacheName, "wb") as f:
                f.write(cacheKey)
                PackagePickler(f, nameFormatter).dump(result)
            os.replace(newCacheName, cacheName)
        except OSError as e:
            print("Error saving internal state:", str(e), file=sys.stderr)

        return result.refDeref([], {}, None, nameFormatter)

    def generatePackages(self, nameFormatter, envOverrides={}, sandboxEnabled=False):
        (env, cacheKey) = self.__getEnvWithCacheKey(envOverrides, sandboxEnabled)
        return PackageSet(cacheKey, self.__aliases, self.__stringFunctions,
            lambda: self.__generatePackages(nameFormatter, env, cacheKey, sandboxEnabled))

    def getPolicy(self, name, location=None):
        (policy, warning) = self.__policies[name]
        if policy is None:
            warning.show(location)
        return policy

    @property
    def sandboxInvariant(self):
        try:
            return self.__sandboxInvariant
        except AttributeError:
            self.__sandboxInvariant = self.getPolicy("sandboxInvariant")
            return self.__sandboxInvariant


class YamlCache:

    def open(self):
        try:
            self.__con = sqlite3.connect(".bob-cache.sqlite3", isolation_level=None)
            self.__cur = self.__con.cursor()
            self.__cur.execute("CREATE TABLE IF NOT EXISTS meta(key PRIMARY KEY, value)")
            self.__cur.execute("CREATE TABLE IF NOT EXISTS yaml(name PRIMARY KEY, stat, digest, data)")

            # check if Bob was changed
            self.__cur.execute("BEGIN")
            self.__cur.execute("SELECT value FROM meta WHERE key='vsn'")
            vsn = self.__cur.fetchone()
            if (vsn is None) or (vsn[0] != BOB_INPUT_HASH):
                # Bob was changed or new workspace -> purge cache
                self.__cur.execute("INSERT OR REPLACE INTO meta VALUES ('vsn', ?)", (BOB_INPUT_HASH,))
                self.__cur.execute("DELETE FROM yaml")
                self.__hot = False
            else:
                # This could work
                self.__hot = True
        except sqlite3.Error as e:
            raise ParseError("Cannot access cache: " + str(e),
                help="You probably executed Bob concurrently in the same workspace. Try again later.")
        self.__files = {}

    def close(self):
        try:
            self.__cur.execute("END")
            self.__cur.close()
            self.__con.close()
        except sqlite3.Error as e:
            raise ParseError("Cannot commit cache: " + str(e),
                help="You probably executed Bob concurrently in the same workspace. Try again later.")
        h = hashlib.sha1()
        for (name, data) in sorted(self.__files.items()):
            h.update(struct.pack("<I", len(name)))
            h.update(name.encode('utf8'))
            h.update(data)
        self.__digest = h.digest()

    def getDigest(self):
        return self.__digest

    def loadYaml(self, name, yamlSchema, default):
        try:
            bs = binStat(name)
            if self.__hot:
                self.__cur.execute("SELECT digest, data FROM yaml WHERE name=? AND stat=?",
                                    (name, bs))
                cached = self.__cur.fetchone()
                if cached is not None:
                    self.__files[name] = cached[0]
                    return pickle.loads(cached[1])

            with open(name, "r", encoding='utf8') as f:
                try:
                    rawData = f.read()
                    data = yaml.safe_load(rawData)
                    digest = hashlib.sha1(rawData.encode('utf8')).digest()
                except Exception as e:
                    raise ParseError("Error while parsing {}: {}".format(name, str(e)))

            if data is None: data = default
            try:
                data = yamlSchema.validate(data)
            except schema.SchemaError as e:
                raise ParseError("Error while validating {}: {}".format(name, str(e)))

            self.__files[name] = digest
            self.__cur.execute("INSERT OR REPLACE INTO yaml VALUES (?, ?, ?, ?)",
                (name, bs, digest, pickle.dumps(data)))
        except sqlite3.Error as e:
            raise ParseError("Cannot access cache: " + str(e),
                help="You probably executed Bob concurrently in the same workspace. Try again later.")
        except OSError as e:
            raise ParseError("Error loading yaml file: " + str(e))

        return data

    def loadBinary(self, name):
        with open(name, "rb") as f:
            result = f.read()
        self.__files[name] = hashlib.sha1(result).digest()
        return result


class PackagePickler(pickle.Pickler):
    def __init__(self, file, pathFormatter):
        super().__init__(file, -1, fix_imports=False)
        self.__pathFormatter = pathFormatter

    def persistent_id(self, obj):
        if obj is self.__pathFormatter:
            return ("pathfmt", None)
        elif isinstance(obj, Recipe):
            return ("recipe", obj.getPackageName())
        else:
            return None

class PackageUnpickler(pickle.Unpickler):
    def __init__(self, file, recipeGetter, plugins, pathFormatter):
        super().__init__(file)
        self.__recipeGetter = recipeGetter
        self.__plugins = plugins
        self.__pathFormatter = pathFormatter

    def persistent_load(self, pid):
        (tag, key) = pid
        if tag == "pathfmt":
            return self.__pathFormatter
        elif tag == "recipe":
            return self.__recipeGetter(key)
        else:
            raise pickle.UnpicklingError("unsupported object")

    def find_class(self, module, name):
        if module.startswith("__bob_plugin_"):
            return getattr(self.__plugins[module], name)
        else:
            return super().find_class(module, name)

