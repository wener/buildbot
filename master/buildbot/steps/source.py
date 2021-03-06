# -*- test-case-name: buildbot.test.test_vc -*-

from warnings import warn
from email.Utils import formatdate
from twisted.python import log
from buildbot.process.buildstep import LoggingBuildStep, LoggedRemoteCommand
from buildbot.interfaces import BuildSlaveTooOldError
from buildbot.status.builder import SKIPPED


class Source(LoggingBuildStep):
    """This is a base class to generate a source tree in the buildslave.
    Each version control system has a specialized subclass, and is expected
    to override __init__ and implement computeSourceRevision() and
    startVC(). The class as a whole builds up the self.args dictionary, then
    starts a LoggedRemoteCommand with those arguments.
    """

    # if the checkout fails, there's no point in doing anything else
    haltOnFailure = True
    flunkOnFailure = True
    notReally = False

    branch = None # the default branch, should be set in __init__

    def __init__(self, workdir=None, mode='update', alwaysUseLatest=False,
                 timeout=20*60, retry=None, **kwargs):
        """
        @type  workdir: string
        @param workdir: local directory (relative to the Builder's root)
                        where the tree should be placed

        @type  mode: string
        @param mode: the kind of VC operation that is desired:
           - 'update': specifies that the checkout/update should be
             performed directly into the workdir. Each build is performed
             in the same directory, allowing for incremental builds. This
             minimizes disk space, bandwidth, and CPU time. However, it
             may encounter problems if the build process does not handle
             dependencies properly (if you must sometimes do a 'clean
             build' to make sure everything gets compiled), or if source
             files are deleted but generated files can influence test
             behavior (e.g. python's .pyc files), or when source
             directories are deleted but generated files prevent CVS from
             removing them. When used with a patched checkout, from a
             previous buildbot try for instance, it will try to "revert"
             the changes first and will do a clobber if it is unable to
             get a clean checkout. The behavior is SCM-dependent.

           - 'copy': specifies that the source-controlled workspace
             should be maintained in a separate directory (called the
             'copydir'), using checkout or update as necessary. For each
             build, a new workdir is created with a copy of the source
             tree (rm -rf workdir; cp -R -P -p copydir workdir). This
             doubles the disk space required, but keeps the bandwidth low
             (update instead of a full checkout). A full 'clean' build
             is performed each time.  This avoids any generated-file
             build problems, but is still occasionally vulnerable to
             problems such as a CVS repository being manually rearranged
             (causing CVS errors on update) which are not an issue with
             a full checkout.

           - 'clobber': specifies that the working directory should be
             deleted each time, necessitating a full checkout for each
             build. This insures a clean build off a complete checkout,
             avoiding any of the problems described above, but is
             bandwidth intensive, as the whole source tree must be
             pulled down for each build.

           - 'export': is like 'clobber', except that e.g. the 'cvs
             export' command is used to create the working directory.
             This command removes all VC metadata files (the
             CVS/.svn/{arch} directories) from the tree, which is
             sometimes useful for creating source tarballs (to avoid
             including the metadata in the tar file). Not all VC systems
             support export.

        @type  alwaysUseLatest: boolean
        @param alwaysUseLatest: whether to always update to the most
        recent available sources for this build.

        Normally the Source step asks its Build for a list of all
        Changes that are supposed to go into the build, then computes a
        'source stamp' (revision number or timestamp) that will cause
        exactly that set of changes to be present in the checked out
        tree. This is turned into, e.g., 'cvs update -D timestamp', or
        'svn update -r revnum'. If alwaysUseLatest=True, bypass this
        computation and always update to the latest available sources
        for each build.

        The source stamp helps avoid a race condition in which someone
        commits a change after the master has decided to start a build
        but before the slave finishes checking out the sources. At best
        this results in a build which contains more changes than the
        buildmaster thinks it has (possibly resulting in the wrong
        person taking the blame for any problems that result), at worst
        is can result in an incoherent set of sources (splitting a
        non-atomic commit) which may not build at all.

        @type  retry: tuple of ints (delay, repeats) (or None)
        @param retry: if provided, VC update failures are re-attempted up
                      to REPEATS times, with DELAY seconds between each
                      attempt. Some users have slaves with poor connectivity
                      to their VC repository, and they say that up to 80% of
                      their build failures are due to transient network
                      failures that could be handled by simply retrying a
                      couple times.

        """

        LoggingBuildStep.__init__(self, **kwargs)
        self.addFactoryArguments(workdir=workdir,
                                 mode=mode,
                                 alwaysUseLatest=alwaysUseLatest,
                                 timeout=timeout,
                                 retry=retry,
                                 )

        assert mode in ("update", "copy", "clobber", "export")
        if retry:
            delay, repeats = retry
            assert isinstance(repeats, int)
            assert repeats > 0
        self.args = {'mode': mode,
                     'workdir': workdir,
                     'timeout': timeout,
                     'retry': retry,
                     'patch': None, # set during .start
                     }
        self.alwaysUseLatest = alwaysUseLatest

        # Compute defaults for descriptions:
        description = ["updating"]
        descriptionDone = ["update"]
        if mode == "clobber":
            description = ["checkout"]
            # because checkingouting takes too much space
            descriptionDone = ["checkout"]
        elif mode == "export":
            description = ["exporting"]
            descriptionDone = ["export"]
        self.description = description
        self.descriptionDone = descriptionDone

    def setStepStatus(self, step_status):
        LoggingBuildStep.setStepStatus(self, step_status)

    def setDefaultWorkdir(self, workdir):
        self.args['workdir'] = self.args['workdir'] or workdir

    def describe(self, done=False):
        if done:
            return self.descriptionDone
        return self.description

    def computeSourceRevision(self, changes):
        """Each subclass must implement this method to do something more
        precise than -rHEAD every time. For version control systems that use
        repository-wide change numbers (SVN, P4), this can simply take the
        maximum such number from all the changes involved in this build. For
        systems that do not (CVS), it needs to create a timestamp based upon
        the latest Change, the Build's treeStableTimer, and an optional
        self.checkoutDelay value."""
        return None

    def computeRepositoryURL(self, repository):
        '''
        Helper function that the repository URL based on the parameter the
        source step took and the Change 'repository' property
        '''

        assert not repository or callable(repository) or isinstance(repository, dict) or \
            isinstance(repository, str) or isinstance(repository, unicode)

        s = self.build.getSourceStamp()
        if not repository:
            assert s.repository
            return str(s.repository)
        else:
            if callable(repository):
                return str(repository(s.repository))
            elif isinstance(repository, dict):
                return str(repository.get(s.repository))
            else: # string or unicode
                try:
                    repourl = str(repository % s.repository)
                except TypeError:
                    # that's the backward compatibility case
                    repourl = repository
                return str(repourl)

    def start(self):
        if self.notReally:
            log.msg("faking %s checkout/update" % self.name)
            self.step_status.setText(["fake", self.name, "successful"])
            self.addCompleteLog("log",
                                "Faked %s checkout/update 'successful'\n" \
                                % self.name)
            return SKIPPED

        # what source stamp would this build like to use?
        s = self.build.getSourceStamp()
        # if branch is None, then use the Step's "default" branch
        branch = s.branch or self.branch
        # if revision is None, use the latest sources (-rHEAD)
        revision = s.revision
        if not revision and not self.alwaysUseLatest:
            revision = self.computeSourceRevision(s.changes)
            # the revision property is currently None, so set it to something
            # more interesting
            self.setProperty('revision', str(revision), "Source")

        # if patch is None, then do not patch the tree after checkout

        # 'patch' is None or a tuple of (patchlevel, diff, root)
        # root is optional.
        patch = s.patch
        if patch:
            self.addCompleteLog("patch", patch[1])

        if self.alwaysUseLatest:
            revision = None
        self.startVC(branch, revision, patch)

    def commandComplete(self, cmd):
        if cmd.updates.has_key("got_revision"):
            got_revision = cmd.updates["got_revision"][-1]
            if got_revision is not None:
                self.setProperty("got_revision", str(got_revision), "Source")



class BK(Source):
    """I perform BitKeeper checkout/update operations."""
    
    name = 'bk'
    
    def __init__(self, bkurl=None, baseURL=None,
                 directory=None, extra_args=None, **kwargs):
        """
        @type  bkurl: string
        @param bkurl: the URL which points to the BitKeeper server.
                 
        @type  baseURL: string
        @param baseURL: if branches are enabled, this is the base URL to
                        which a branch name will be appended. It should
                        probably end in a slash. Use exactly one of
                        C{bkurl} and C{baseURL}.
        """
                        
        self.bkurl = bkurl
        self.baseURL = baseURL
        self.extra_args = extra_args
        
        Source.__init__(self, **kwargs)
        self.addFactoryArguments(bkurl=bkurl,
                                 baseURL=baseURL,
                                 directory=directory,
                                 extra_args=extra_args,
                                 )

        if bkurl and baseURL:
            raise ValueError("you must use exactly one of bkurl and baseURL")

        
    def computeSourceRevision(self, changes):
        return changes.revision
                       
                       
    def startVC(self, branch, revision, patch):

        warnings = []
        slavever = self.slaveVersion("bk")
        if not slavever:
            m = "slave does not have the 'bk' command"
            raise BuildSlaveTooOldError(m)

        if self.bkurl:
            assert not branch # we need baseURL= to use branches
            self.args['bkurl'] = self.computeRepositoryURL(self.bkurl)
        else:
            self.args['bkurl'] = self.computeRepositoryURL(self.baseURL) + branch
        self.args['revision'] = revision
        self.args['patch'] = patch
        self.args['branch'] = branch
        if self.extra_args is not None:
            self.args['extra_args'] = self.extra_args

        revstuff = []
        revstuff.append("[branch]")
        if revision is not None:
            revstuff.append("r%s" % revision)
        if patch is not None:
            revstuff.append("[patch]")
        self.description.extend(revstuff)
        self.descriptionDone.extend(revstuff)

        cmd = LoggedRemoteCommand("bk", self.args)
        self.startCommand(cmd, warnings)



class CVS(Source):
    """I do CVS checkout/update operations.

    Note: if you are doing anonymous/pserver CVS operations, you will need
    to manually do a 'cvs login' on each buildslave before the slave has any
    hope of success. XXX: fix then, take a cvs password as an argument and
    figure out how to do a 'cvs login' on each build
    """

    name = "cvs"

    #progressMetrics = ('output',)
    #
    # additional things to track: update gives one stderr line per directory
    # (starting with 'cvs server: Updating ') (and is fairly stable if files
    # is empty), export gives one line per directory (starting with 'cvs
    # export: Updating ') and another line per file (starting with U). Would
    # be nice to track these, requires grepping LogFile data for lines,
    # parsing each line. Might be handy to have a hook in LogFile that gets
    # called with each complete line.

    def __init__(self, cvsroot=None, cvsmodule="",
                 global_options=[], branch=None, checkoutDelay=None,
                 checkout_options=[], export_options=[], extra_options=[],
                 login=None,
                 **kwargs):

        """
        @type  cvsroot: string
        @param cvsroot: CVS Repository from which the source tree should
                        be obtained. '/home/warner/Repository' for local
                        or NFS-reachable repositories,
                        ':pserver:anon@foo.com:/cvs' for anonymous CVS,
                        'user@host.com:/cvs' for non-anonymous CVS or
                        CVS over ssh. Lots of possibilities, check the
                        CVS documentation for more.

        @type  cvsmodule: string
        @param cvsmodule: subdirectory of CVS repository that should be
                          retrieved

        @type  login: string or None
        @param login: if not None, a string which will be provided as a
                      password to the 'cvs login' command, used when a
                      :pserver: method is used to access the repository.
                      This login is only needed once, but must be run
                      each time (just before the CVS operation) because
                      there is no way for the buildslave to tell whether
                      it was previously performed or not.

        @type  branch: string
        @param branch: the default branch name, will be used in a '-r'
                       argument to specify which branch of the source tree
                       should be used for this checkout. Defaults to None,
                       which means to use 'HEAD'.

        @type  checkoutDelay: int or None
        @param checkoutDelay: if not None, the number of seconds to put
                              between the last known Change and the
                              timestamp given to the -D argument. This
                              defaults to exactly half of the parent
                              Build's .treeStableTimer, but it could be
                              set to something else if your CVS change
                              notification has particularly weird
                              latency characteristics.

        @type  global_options: list of strings
        @param global_options: these arguments are inserted in the cvs
                               command line, before the
                               'checkout'/'update' command word. See
                               'cvs --help-options' for a list of what
                               may be accepted here.  ['-r'] will make
                               the checked out files read only. ['-r',
                               '-R'] will also assume the repository is
                               read-only (I assume this means it won't
                               use locks to insure atomic access to the
                               ,v files).

        @type  checkout_options: list of strings
        @param checkout_options: these arguments are inserted in the cvs
                               command line, after 'checkout' but before
                               branch or revision specifiers.

        @type  export_options: list of strings
        @param export_options: these arguments are inserted in the cvs
                               command line, after 'export' but before
                               branch or revision specifiers.

        @type  extra_options: list of strings
        @param extra_options: these arguments are inserted in the cvs
                               command line, after 'checkout' or 'export' but before
                               branch or revision specifiers.
                               """

        self.checkoutDelay = checkoutDelay
        self.branch = branch
        self.cvsroot = cvsroot

        Source.__init__(self, **kwargs)
        self.addFactoryArguments(cvsroot=cvsroot,
                                 cvsmodule=cvsmodule,
                                 global_options=global_options,
                                 checkout_options=checkout_options,
                                 export_options=export_options,
                                 extra_options=extra_options,
                                 branch=branch,
                                 checkoutDelay=checkoutDelay,
                                 login=login,
                                 )

        self.args.update({'cvsmodule': cvsmodule,
                          'global_options': global_options,
                          'checkout_options':checkout_options,
                          'export_options':export_options,
                          'extra_options':extra_options,
                          'login': login,
                          })

    def computeSourceRevision(self, changes):
        if not changes:
            return None
        lastChange = max([c.when for c in changes])
        if self.checkoutDelay is not None:
            when = lastChange + self.checkoutDelay
        else:
            lastSubmit = max([r.submittedAt for r in self.build.requests])
            when = (lastChange + lastSubmit) / 2
        return formatdate(when)

    def startVC(self, branch, revision, patch):
        if self.slaveVersionIsOlderThan("cvs", "1.39"):
            # the slave doesn't know to avoid re-using the same sourcedir
            # when the branch changes. We have no way of knowing which branch
            # the last build used, so if we're using a non-default branch and
            # either 'update' or 'copy' modes, it is safer to refuse to
            # build, and tell the user they need to upgrade the buildslave.
            if (branch != self.branch
                and self.args['mode'] in ("update", "copy")):
                m = ("This buildslave (%s) does not know about multiple "
                     "branches, and using mode=%s would probably build the "
                     "wrong tree. "
                     "Refusing to build. Please upgrade the buildslave to "
                     "buildbot-0.7.0 or newer." % (self.build.slavename,
                                                   self.args['mode']))
                log.msg(m)
                raise BuildSlaveTooOldError(m)

        if self.slaveVersionIsOlderThan("cvs", "2.10"):
            if self.args['extra_options'] or self.args['export_options']:
                m = ("This buildslave (%s) does not support export_options "
                     "or extra_options arguments to the CVS step."
                     % (self.build.slavename))
                log.msg(m)
                raise BuildSlaveTooOldError(m)
            # the unwanted args are empty, and will probably be ignored by
            # the slave, but delete them just to be safe
            del self.args['export_options']
            del self.args['extra_options']

        if branch is None:
            branch = "HEAD"
        self.args['cvsroot'] = self.computeRepositoryURL(self.cvsroot)
        self.args['branch'] = branch
        self.args['revision'] = revision
        self.args['patch'] = patch

        if self.args['branch'] == "HEAD" and self.args['revision']:
            # special case. 'cvs update -r HEAD -D today' gives no files
            # TODO: figure out why, see if it applies to -r BRANCH
            self.args['branch'] = None

        # deal with old slaves
        warnings = []
        slavever = self.slaveVersion("cvs", "old")

        if slavever == "old":
            # 0.5.0
            if self.args['mode'] == "export":
                self.args['export'] = 1
            elif self.args['mode'] == "clobber":
                self.args['clobber'] = 1
            elif self.args['mode'] == "copy":
                self.args['copydir'] = "source"
            self.args['tag'] = self.args['branch']
            assert not self.args['patch'] # 0.5.0 slave can't do patch

        cmd = LoggedRemoteCommand("cvs", self.args)
        self.startCommand(cmd, warnings)


class SVN(Source):
    """I perform Subversion checkout/update operations."""

    name = 'svn'

    def __init__(self, svnurl=None, baseURL=None, defaultBranch=None,
                 directory=None, username=None, password=None,
                 extra_args=None, keep_on_purge=None, ignore_ignores=None,
                 always_purge=None, depth=None, **kwargs):
        """
        @type  svnurl: string
        @param svnurl: the URL which points to the Subversion server,
                       combining the access method (HTTP, ssh, local file),
                       the repository host/port, the repository path, the
                       sub-tree within the repository, and the branch to
                       check out. Use exactly one of C{svnurl} and C{baseURL}.

        @param baseURL: if branches are enabled, this is the base URL to
                        which a branch name will be appended. It should
                        probably end in a slash. Use exactly one of
                        C{svnurl} and C{baseURL}.

        @param defaultBranch: if branches are enabled, this is the branch
                              to use if the Build does not specify one
                              explicitly. It will simply be appended
                              to C{baseURL} and the result handed to
                              the SVN command.

        @type  username: string
        @param username: username to pass to svn's --username

        @type  password: string
        @param password: password to pass to svn's --password
        """

        if not 'workdir' in kwargs and directory is not None:
            # deal with old configs
            warn("Please use workdir=, not directory=", DeprecationWarning)
            kwargs['workdir'] = directory

        self.svnurl = svnurl
        self.baseURL = baseURL
        self.branch = defaultBranch
        self.username = username
        self.password = password
        self.extra_args = extra_args
        self.keep_on_purge = keep_on_purge
        self.ignore_ignores = ignore_ignores
        self.always_purge = always_purge
        self.depth = depth

        Source.__init__(self, **kwargs)
        self.addFactoryArguments(svnurl=svnurl,
                                 baseURL=baseURL,
                                 defaultBranch=defaultBranch,
                                 directory=directory,
                                 username=username,
                                 password=password,
                                 extra_args=extra_args,
                                 keep_on_purge=keep_on_purge,
                                 ignore_ignores=ignore_ignores,
                                 always_purge=always_purge,
                                 depth=depth,
                                 )

        if svnurl and baseURL:
            raise ValueError("you must use either svnurl OR baseURL")

    def computeSourceRevision(self, changes):
        if not changes or None in [c.revision for c in changes]:
            return None
        lastChange = max([int(c.revision) for c in changes])
        return lastChange

    def startVC(self, branch, revision, patch):

        # handle old slaves
        warnings = []
        slavever = self.slaveVersion("svn", "old")
        if not slavever:
            m = "slave does not have the 'svn' command"
            raise BuildSlaveTooOldError(m)

        if self.slaveVersionIsOlderThan("svn", "1.39"):
            # the slave doesn't know to avoid re-using the same sourcedir
            # when the branch changes. We have no way of knowing which branch
            # the last build used, so if we're using a non-default branch and
            # either 'update' or 'copy' modes, it is safer to refuse to
            # build, and tell the user they need to upgrade the buildslave.
            if (branch != self.branch
                and self.args['mode'] in ("update", "copy")):
                m = ("This buildslave (%s) does not know about multiple "
                     "branches, and using mode=%s would probably build the "
                     "wrong tree. "
                     "Refusing to build. Please upgrade the buildslave to "
                     "buildbot-0.7.0 or newer." % (self.build.slavename,
                                                   self.args['mode']))
                raise BuildSlaveTooOldError(m)

        if slavever == "old":
            # 0.5.0 compatibility
            if self.args['mode'] in ("clobber", "copy"):
                # TODO: use some shell commands to make up for the
                # deficiency, by blowing away the old directory first (thus
                # forcing a full checkout)
                warnings.append("WARNING: this slave can only do SVN updates"
                                ", not mode=%s\n" % self.args['mode'])
                log.msg("WARNING: this slave only does mode=update")
            if self.args['mode'] == "export":
                raise BuildSlaveTooOldError("old slave does not have "
                                            "mode=export")
            self.args['directory'] = self.args['workdir']
            if revision is not None:
                # 0.5.0 can only do HEAD. We have no way of knowing whether
                # the requested revision is HEAD or not, and for
                # slowly-changing trees this will probably do the right
                # thing, so let it pass with a warning
                m = ("WARNING: old slave can only update to HEAD, not "
                     "revision=%s" % revision)
                log.msg(m)
                warnings.append(m + "\n")
            revision = "HEAD" # interprets this key differently
            if patch:
                raise BuildSlaveTooOldError("old slave can't do patch")

        if self.svnurl:
            self.args['svnurl'] = self.computeRepositoryURL(self.svnurl)
        else:
            self.args['svnurl'] = (self.computeRepositoryURL(self.baseURL) +
                                   branch)
        self.args['revision'] = revision
        self.args['patch'] = patch

        self.args['always_purge'] = self.always_purge

        #Set up depth if specified
        if self.depth is not None:
            if self.slaveVersionIsOlderThan("svn","2.9"):
                m = ("This buildslave (%s) does not support svn depth "
                     "arguments.  Refusing to build. "
                     "Please upgrade the buildslave." % (self.build.slavename))
                raise BuildSlaveTooOldError(m)
            else: 
                self.args['depth'] = self.depth

        if self.username is not None or self.password is not None:
            if self.slaveVersionIsOlderThan("svn", "2.8"):
                m = ("This buildslave (%s) does not support svn usernames "
                     "and passwords.  "
                     "Refusing to build. Please upgrade the buildslave to "
                     "buildbot-0.7.10 or newer." % (self.build.slavename,))
                raise BuildSlaveTooOldError(m)
            if self.username is not None:
                self.args['username'] = self.username
            if self.password is not None:
                self.args['password'] = self.password

        if self.extra_args is not None:
            self.args['extra_args'] = self.extra_args

        revstuff = []
        #revstuff.append(self.args['svnurl'])
        if self.args['svnurl'].find('trunk') == -1:
            revstuff.append("[branch]")
        if revision is not None:
            revstuff.append("r%s" % revision)
        if patch is not None:
            revstuff.append("[patch]")
        self.description.extend(revstuff)
        self.descriptionDone.extend(revstuff)

        cmd = LoggedRemoteCommand("svn", self.args)
        self.startCommand(cmd, warnings)


class Darcs(Source):
    """Check out a source tree from a Darcs repository at 'repourl'.

    Darcs has no concept of file modes. This means the eXecute-bit will be
    cleared on all source files. As a result, you may need to invoke
    configuration scripts with something like:

    C{s(step.Configure, command=['/bin/sh', './configure'])}
    """

    name = "darcs"

    def __init__(self, repourl=None, baseURL=None, defaultBranch=None,
                 **kwargs):
        """
        @type  repourl: string
        @param repourl: the URL which points at the Darcs repository. This
                        is used as the default branch. Using C{repourl} does
                        not enable builds of alternate branches: use
                        C{baseURL} to enable this. Use either C{repourl} or
                        C{baseURL}, not both.

        @param baseURL: if branches are enabled, this is the base URL to
                        which a branch name will be appended. It should
                        probably end in a slash. Use exactly one of
                        C{repourl} and C{baseURL}.

        @param defaultBranch: if branches are enabled, this is the branch
                              to use if the Build does not specify one
                              explicitly. It will simply be appended to
                              C{baseURL} and the result handed to the
                              'darcs pull' command.
        """
        self.repourl = repourl
        self.baseURL = baseURL
        self.branch = defaultBranch
        Source.__init__(self, **kwargs)
        self.addFactoryArguments(repourl=repourl,
                                 baseURL=baseURL,
                                 defaultBranch=defaultBranch,
                                 )
        assert self.args['mode'] != "export", \
               "Darcs does not have an 'export' mode"
        if repourl and baseURL:
            raise ValueError("you must provide exactly one of repourl and"
                             " baseURL")

    def startVC(self, branch, revision, patch):
        slavever = self.slaveVersion("darcs")
        if not slavever:
            m = "slave is too old, does not know about darcs"
            raise BuildSlaveTooOldError(m)

        if self.slaveVersionIsOlderThan("darcs", "1.39"):
            if revision:
                # TODO: revisit this once we implement computeSourceRevision
                m = "0.6.6 slaves can't handle args['revision']"
                raise BuildSlaveTooOldError(m)

            # the slave doesn't know to avoid re-using the same sourcedir
            # when the branch changes. We have no way of knowing which branch
            # the last build used, so if we're using a non-default branch and
            # either 'update' or 'copy' modes, it is safer to refuse to
            # build, and tell the user they need to upgrade the buildslave.
            if (branch != self.branch
                and self.args['mode'] in ("update", "copy")):
                m = ("This buildslave (%s) does not know about multiple "
                     "branches, and using mode=%s would probably build the "
                     "wrong tree. "
                     "Refusing to build. Please upgrade the buildslave to "
                     "buildbot-0.7.0 or newer." % (self.build.slavename,
                                                   self.args['mode']))
                raise BuildSlaveTooOldError(m)

        if self.repourl:
            assert not branch # we need baseURL= to use branches
            self.args['repourl'] = self.computeRepositoryURL(self.repourl)
        else:
            self.args['repourl'] = self.computeRepositoryURL(self.baseURL) + branch
        self.args['revision'] = revision
        self.args['patch'] = patch

        revstuff = []
        if branch is not None and branch != self.branch:
            revstuff.append("[branch]")
        self.description.extend(revstuff)
        self.descriptionDone.extend(revstuff)

        cmd = LoggedRemoteCommand("darcs", self.args)
        self.startCommand(cmd)


class Git(Source):
    """Check out a source tree from a git repository 'repourl'."""

    name = "git"

    def __init__(self, repourl=None,
                 branch="master",
                 submodules=False,
                 ignore_ignores=None,
                 shallow=False,
                 **kwargs):
        """
        @type  repourl: string
        @param repourl: the URL which points at the git repository

        @type  branch: string
        @param branch: The branch or tag to check out by default. If
                       a build specifies a different branch, it will
                       be used instead of this.

        @type  submodules: boolean
        @param submodules: Whether or not to update (and initialize)
                       git submodules.

        @type  shallow: boolean
        @param shallow: Use a shallow or clone, if possible
        """
        Source.__init__(self, **kwargs)
        self.repourl = repourl
        self.addFactoryArguments(repourl=repourl,
                                 branch=branch,
                                 submodules=submodules,
                                 ignore_ignores=ignore_ignores,
                                 shallow=shallow,
                                 )
        self.args.update({'branch': branch,
                          'submodules': submodules,
                          'ignore_ignores': ignore_ignores,
                          'shallow': shallow,
                          })

    def computeSourceRevision(self, changes):
        if not changes:
            return None
        return changes[-1].revision

    def startVC(self, branch, revision, patch):
        if branch is not None:
            self.args['branch'] = branch

        self.args['repourl'] = self.computeRepositoryURL(self.repourl)                  
        self.args['revision'] = revision
        self.args['patch'] = patch
        slavever = self.slaveVersion("git")
        if not slavever:
            raise BuildSlaveTooOldError("slave is too old, does not know "
                                        "about git")
        cmd = LoggedRemoteCommand("git", self.args)
        self.startCommand(cmd)


class Bzr(Source):
    """Check out a source tree from a bzr (Bazaar) repository at 'repourl'.

    """

    name = "bzr"

    def __init__(self, repourl=None, baseURL=None, defaultBranch=None,
                 forceSharedRepo=None,
                 **kwargs):
        """
        @type  repourl: string
        @param repourl: the URL which points at the bzr repository. This
                        is used as the default branch. Using C{repourl} does
                        not enable builds of alternate branches: use
                        C{baseURL} to enable this. Use either C{repourl} or
                        C{baseURL}, not both.

        @param baseURL: if branches are enabled, this is the base URL to
                        which a branch name will be appended. It should
                        probably end in a slash. Use exactly one of
                        C{repourl} and C{baseURL}.

        @param defaultBranch: if branches are enabled, this is the branch
                              to use if the Build does not specify one
                              explicitly. It will simply be appended to
                              C{baseURL} and the result handed to the
                              'bzr checkout pull' command.


        @param forceSharedRepo: Boolean, defaults to False. If set to True,
                                the working directory will be made into a
                                bzr shared repository if it is not already.
                                Shared repository greatly reduces the amount
                                of history data that needs to be downloaded
                                if not using update/copy mode, or if using
                                update/copy mode with multiple branches.
        """
        self.repourl = repourl
        self.baseURL = baseURL
        self.branch = defaultBranch
        Source.__init__(self, **kwargs)
        self.addFactoryArguments(repourl=repourl,
                                 baseURL=baseURL,
                                 defaultBranch=defaultBranch,
                                 forceSharedRepo=forceSharedRepo
                                 )
        self.args.update({'forceSharedRepo': forceSharedRepo})
        if repourl and baseURL:
            raise ValueError("you must provide exactly one of repourl and"
                             " baseURL")

    def computeSourceRevision(self, changes):
        if not changes:
            return None
        lastChange = max([int(c.revision) for c in changes])
        return lastChange

    def startVC(self, branch, revision, patch):
        slavever = self.slaveVersion("bzr")
        if not slavever:
            m = "slave is too old, does not know about bzr"
            raise BuildSlaveTooOldError(m)

        if self.repourl:
            assert not branch # we need baseURL= to use branches
            self.args['repourl'] = self.computeRepositoryURL(self.repourl)
        else:
            self.args['repourl'] = self.computeRepositoryURL(self.baseURL) + branch
        self.args['revision'] = revision
        self.args['patch'] = patch

        revstuff = []
        if branch is not None and branch != self.branch:
            revstuff.append("[" + branch + "]")
        if revision is not None:
            revstuff.append("r%s" % revision)
        self.description.extend(revstuff)
        self.descriptionDone.extend(revstuff)

        cmd = LoggedRemoteCommand("bzr", self.args)
        self.startCommand(cmd)


class Mercurial(Source):
    """Check out a source tree from a mercurial repository 'repourl'."""

    name = "hg"

    def __init__(self, repourl=None, baseURL=None, defaultBranch=None,
                 branchType='dirname', clobberOnBranchChange=True, **kwargs):
        """
        @type  repourl: string
        @param repourl: the URL which points at the Mercurial repository.
                        This uses the 'default' branch unless defaultBranch is
                        specified below and the C{branchType} is set to
                        'inrepo'.  It is an error to specify a branch without
                        setting the C{branchType} to 'inrepo'.

        @param baseURL: if 'dirname' branches are enabled, this is the base URL
                        to which a branch name will be appended. It should
                        probably end in a slash.  Use exactly one of C{repourl}
                        and C{baseURL}.

        @param defaultBranch: if branches are enabled, this is the branch
                              to use if the Build does not specify one
                              explicitly.
                              For 'dirname' branches, It will simply be
                              appended to C{baseURL} and the result handed to
                              the 'hg update' command.
                              For 'inrepo' branches, this specifies the named
                              revision to which the tree will update after a
                              clone.

        @param branchType: either 'dirname' or 'inrepo' depending on whether
                           the branch name should be appended to the C{baseURL}
                           or the branch is a mercurial named branch and can be
                           found within the C{repourl}

        @param clobberOnBranchChange: boolean, defaults to True. If set and
                                      using inrepos branches, clobber the tree
                                      at each branch change. Otherwise, just
                                      update to the branch.
        """
        self.repourl = repourl
        self.baseURL = baseURL
        self.branch = defaultBranch
        self.branchType = branchType
        self.clobberOnBranchChange = clobberOnBranchChange
        Source.__init__(self, **kwargs)
        self.addFactoryArguments(repourl=repourl,
                                 baseURL=baseURL,
                                 defaultBranch=defaultBranch,
                                 branchType=branchType,
                                 clobberOnBranchChange=clobberOnBranchChange,
                                 )
        if repourl and baseURL:
            raise ValueError("you must provide exactly one of repourl and"
                             " baseURL")

    def startVC(self, branch, revision, patch):
        slavever = self.slaveVersion("hg")
        if not slavever:
            raise BuildSlaveTooOldError("slave is too old, does not know "
                                        "about hg")

        if self.repourl:
            # we need baseURL= to use dirname branches
            assert self.branchType == 'inrepo' or not branch
            self.args['repourl'] = self.computeRepositoryURL(self.repourl)
            if branch:
                self.args['branch'] = branch
        else:
            self.args['repourl'] = self.computeRepositoryURL(self.baseURL) + (branch or '')
        self.args['revision'] = revision
        self.args['patch'] = patch
        self.args['clobberOnBranchChange'] = self.clobberOnBranchChange
        self.args['branchType'] = self.branchType

        revstuff = []
        if branch is not None and branch != self.branch:
            revstuff.append("[branch]")
        self.description.extend(revstuff)
        self.descriptionDone.extend(revstuff)

        cmd = LoggedRemoteCommand("hg", self.args)
        self.startCommand(cmd)

    def computeSourceRevision(self, changes):
        if not changes:
            return None
        # without knowing the revision ancestry graph, we can't sort the
        # changes at all. So for now, assume they were given to us in sorted
        # order, and just pay attention to the last one. See ticket #103 for
        # more details.
        if len(changes) > 1:
            log.msg("Mercurial.computeSourceRevision: warning: "
                    "there are %d changes here, assuming the last one is "
                    "the most recent" % len(changes))
        return changes[-1].revision


class P4(Source):
    """ P4 is a class for accessing perforce revision control"""
    name = "p4"

    def __init__(self, p4base=None, defaultBranch=None, p4port=None, p4user=None,
                 p4passwd=None, p4extra_views=[], p4line_end='local',
                 p4client='buildbot_%(slave)s_%(builder)s', **kwargs):
        """
        @type  p4base: string
        @param p4base: A view into a perforce depot, typically
                       "//depot/proj/"

        @type  defaultBranch: string
        @param defaultBranch: Identify a branch to build by default. Perforce
                              is a view based branching system. So, the branch
                              is normally the name after the base. For example,
                              branch=1.0 is view=//depot/proj/1.0/...
                              branch=1.1 is view=//depot/proj/1.1/...

        @type  p4port: string
        @param p4port: Specify the perforce server to connection in the format
                       <host>:<port>. Example "perforce.example.com:1666"

        @type  p4user: string
        @param p4user: The perforce user to run the command as.

        @type  p4passwd: string
        @param p4passwd: The password for the perforce user.

        @type  p4extra_views: list of tuples
        @param p4extra_views: Extra views to be added to
                              the client that is being used.

        @type  p4line_end: string
        @param p4line_end: value of the LineEnd client specification property

        @type  p4client: string
        @param p4client: The perforce client to use for this buildslave.
        """

        self.p4base = p4base
        self.branch = defaultBranch
        Source.__init__(self, **kwargs)
        self.addFactoryArguments(p4base=p4base,
                                 defaultBranch=defaultBranch,
                                 p4port=p4port,
                                 p4user=p4user,
                                 p4passwd=p4passwd,
                                 p4extra_views=p4extra_views,
                                 p4line_end=p4line_end,
                                 p4client=p4client,
                                 )
        self.args['p4port'] = p4port
        self.args['p4user'] = p4user
        self.args['p4passwd'] = p4passwd
        self.args['p4extra_views'] = p4extra_views
        self.args['p4line_end'] = p4line_end
        self.p4client = p4client

    def setBuild(self, build):
        Source.setBuild(self, build)
        self.args['p4base'] = self.computeRepositoryURL(self.p4base)
        self.args['p4client'] = self.p4client % {
            'slave': build.slavename,
            'builder': build.builder.name,
        }

    def computeSourceRevision(self, changes):
        if not changes:
            return None
        lastChange = max([int(c.revision) for c in changes])
        return lastChange

    def startVC(self, branch, revision, patch):
        slavever = self.slaveVersion("p4")
        assert slavever, "slave is too old, does not know about p4"
        args = dict(self.args)
        args['branch'] = branch or self.branch
        args['revision'] = revision
        args['patch'] = patch
        cmd = LoggedRemoteCommand("p4", args)
        self.startCommand(cmd)

class P4Sync(Source):
    """This is a partial solution for using a P4 source repository. You are
    required to manually set up each build slave with a useful P4
    environment, which means setting various per-slave environment variables,
    and creating a P4 client specification which maps the right files into
    the slave's working directory. Once you have done that, this step merely
    performs a 'p4 sync' to update that workspace with the newest files.

    Each slave needs the following environment:

     - PATH: the 'p4' binary must be on the slave's PATH
     - P4USER: each slave needs a distinct user account
     - P4CLIENT: each slave needs a distinct client specification

    You should use 'p4 client' (?) to set up a client view spec which maps
    the desired files into $SLAVEBASE/$BUILDERBASE/source .
    """

    name = "p4sync"

    def __init__(self, p4port, p4user, p4passwd, p4client, **kwargs):
        assert kwargs['mode'] == "copy", "P4Sync can only be used in mode=copy"
        self.branch = None
        Source.__init__(self, **kwargs)
        self.addFactoryArguments(p4port=p4port,
                                 p4user=p4user,
                                 p4passwd=p4passwd,
                                 p4client=p4client,
                                )
        self.args['p4port'] = p4port
        self.args['p4user'] = p4user
        self.args['p4passwd'] = p4passwd
        self.args['p4client'] = p4client

    def computeSourceRevision(self, changes):
        if not changes:
            return None
        lastChange = max([int(c.revision) for c in changes])
        return lastChange

    def startVC(self, branch, revision, patch):
        slavever = self.slaveVersion("p4sync")
        assert slavever, "slave is too old, does not know about p4"
        cmd = LoggedRemoteCommand("p4sync", self.args)
        self.startCommand(cmd)
