'''post changesets to a reviewboard server'''

import os, errno, re
import cStringIO
from hgversion import HgVersion
from mercurial import cmdutil, hg, ui, mdiff, patch, util, node, scmutil
from mercurial.i18n import _
import sys
from reviewboard import make_rbclient, ReviewBoardError

cmdtable = {}

try:
    from mercurial import registrar
    command = registrar.command(cmdtable)
except (ImportError, AttributeError):
    # Dropped in 4.6
    command = cmdutil.command(cmdtable)

@command('postreview',
        [('o', 'outgoing', False,
         _('use upstream repository to determine the parent diff base')),
        ('O', 'outgoingrepo', '',
         _('use specified repository to determine the parent diff base')),
        ('i', 'repoid', '',
         _('specify repository id or name on reviewboard server')),
        ('m', 'master', '',
         _('use specified revision as the parent diff base')),
        ('', 'server', '', _('ReviewBoard server URL')),
        ('g', 'git', False,
         _('use git extended diff format (enables rename/copy support)')),
        ('e', 'existing', '', _('existing request ID to update')),
        ('u', 'update', False, _('update the fields of an existing request')),
        ('p', 'publish', None, _('publish request immediately')),
        ('', 'parent', '', _('parent revision for the uploaded diff')),
        ('l','longdiff', False,
         _('review all changes since last upstream sync')),
        ('s', 'summary', '', _('summary for the review request')),
        ('d', 'description', '', _('description for the review request')),
        ('U', 'target_people', [], _('comma separated list of people needed to review the code')),
        ('G', 'target_groups', [], _('comma separated list of groups needed to review the code')),
        ('b', 'bugs_closed', [], _('comma separated list of bug numbers')),
        ('w', 'webbrowser', False, _('launch browser to show review')),
        ('', 'username', '', _('username for the ReviewBoard site')),
        ('', 'password', '', _('password for the ReviewBoard site')),
        ('', 'submit_as', '', _('The optional user to submit the review request as.')),
        ('', 'apiver', '', _('ReviewBoard API version (e.g. 1.0, 2.0)')),
        ('', 'apitrace', False, _('Output all API requests and responses to the console')),
        ('I', 'include', [], _('include names matching the given patterns'), _('PATTERN')),
        ('X', 'exclude', [], _('exclude names matching the given patterns'), _('PATTERN')),
        ('', 'disable_ssl_verification', False, _('disable SSL certificate verification')),
        ('W', 'working_directory', False, _('produce diff against current working directory'))
        ],
        _('[options] [REVISION]'))
def postreview(ui, repo, rev='.', **opts):
    '''post a changeset to a Review Board server

This command creates a new review request on a Review Board server, or updates
an existing review request, based on a changeset in the repository. If no
revision number is specified the parent revision of the working directory is
used.

By default, the diff uploaded to the server is based on the parent of the
revision to be reviewed. A different parent may be specified using the
--parent or --longdiff options. --parent r specifies the revision to use on the
left side while --longdiff looks at the upstream repository specified in .hg/hgrc
to find a common ancestor to use on the left side. --parent may need one of
the options below if the Review Board server can't see the parent.

If the parent revision is not available to the Review Board server (e.g. it
exists in your local repository but not in the one that Review Board has
access to) you must tell postreview how to determine the base revision
to use for a parent diff. The --outgoing, --outgoingrepo or --master options
may be used for this purpose. The --outgoing option is the simplest of these;
it assumes that the upstream repository specified in .hg/hgrc is the same as
the one known to Review Board. The other two options offer more control if
this is not the case. In these cases two diffs are uploaded to Review Board:
the first is the difference between Reviewboard's view of the repo and your
parent revision(left side), the second is the difference between your parent
revision and your review revision(right side). Only the second diff is
under review. If you wish to review all the changes local to your repo use
the --longdiff option above.

The --outgoing option recognizes the path entries 'reviewboard', 'default-push'
and 'default' in this order of precedence. 'reviewboard' may be used if the
repository accessible to Review Board is not the upstream repository.

The --git option causes postreview to generate diffs in Git extended format,
which includes information about file renames and copies. ReviewBoard 1.6 beta
2 or later is required in order to use this feature.

The --submit_as option allows to submit the review request as another user.
This requires that the actual logged in user is either a superuser or has the
"reviews.can_submit_as_another_user" permission.

The reviewboard extension may be configured by adding a [reviewboard] section
to your .hgrc or mercurial.ini file, or to the .hg/hgrc file of an individual
repository. The following options are available::

  [reviewboard]

  # REQUIRED
  server = <server_url>             # The URL of your ReviewBoard server

  # OPTIONAL
  http_proxy = <proxy_url>          # HTTP proxy to use for the connection
  user = <rb_username>              # Username to use for ReviewBoard
                                    # connections
  password = <rb_password>          # Password to use for ReviewBoard
                                    # connections
  repoid = <repoid>                 # ReviewBoard repository ID (normally only
                                    # useful in a repository-specific hgrc)
  target_groups = <groups>          # Default groups for new review requests
                                    # (comma-separated list)
  target_people = <users>           # Default users for new review requests
                                    # (comma-separated list)
  explicit_publish_update = <bool>  # If True, updates posted using the -e
                                    # option will not be published immediately
                                    # unless the -p option is also used
  launch_webbrowser = <bool>        # If True, new or updated requests will
                                    # always be shown in a web browser after
                                    # posting.
  encoding = <system_encoding>      # The Encoding of TortoiseHG form
  disable_ssl_verification = <bool> # If True, SSL verification is disabled.
'''

    server = opts.get('server')
    if not server:
        server = ui.config('reviewboard', 'server')

    if not server:
        raise util.Abort(
                _('please specify a reviewboard server in your .hgrc file') )

    encoding = ui.config('reviewboard', 'encoding')
    if not encoding:
        # use default system encoding if no config option
        encoding = sys.stdin.encoding

    '''We are going to fetch the setting string from hg prefs, there we can set
    our own proxy, or specify 'none' to pass an empty dictionary to urllib2
    which overides the default autodetection when we want to force no proxy'''
    http_proxy = ui.config('reviewboard', 'http_proxy' )
    if http_proxy:
        if http_proxy == 'none':
            proxy = {}
        else:
            proxy = { 'http':http_proxy }
    else:
        proxy=None

    disable_ssl_verification = opts.get('disable_ssl_verification')
    if not disable_ssl_verification:
        disable_ssl_verification = ui.config('reviewboard', 'disable_ssl_verification')

    def getdiff(ui, repo, r, parent, opts):
        '''return diff for the specified revision'''
        output = ""
        if opts.get('git') or ui.configbool('diff', 'git'):
            # Git diffs don't include the revision numbers with each file, so
            # we have to put them in the header instead.
            output += "# Node ID " + node.hex(r.node()) + "\n"
            output += "# Parent  " + node.hex(parent.node()) + "\n"
        diffopts = patch.diffopts(ui, opts)
        m = scmutil.match(repo[r.node()], None, opts)
        if opts.get('working_directory'):
            compare_cset = r.node()
            compare_to = None
        else:
            compare_cset = parent.node()
            compare_to = r.node()
        for chunk in patch.diff(repo, compare_cset, compare_to, m, opts=diffopts):
            output += chunk

        return output

    parent = opts.get('parent')
    if parent:
        parent = repo[parent]
    else:
        parent = repo[rev].parents()[0]

    outgoing = opts.get('outgoing')
    outgoingrepo = opts.get('outgoingrepo')
    master = opts.get('master')
    repo_id_opt = opts.get('repoid')
    longdiff = opts.get('longdiff')

    if not repo_id_opt:
        repo_id_opt = ui.config('reviewboard','repoid')

    if master:
        rparent = repo[master]
    elif outgoingrepo:
        rparent = remoteparent(ui, repo, opts, rev, upstream=outgoingrepo)
    elif outgoing:
        rparent = remoteparent(ui, repo, opts, rev)
    elif longdiff:
        parent = remoteparent(ui, repo, opts, rev)
        rparent = None
    else:
        rparent = None

    ui.debug(_('Parent is %s\n' % parent))
    ui.debug(_('Remote parent is %s\n' % rparent))

    request_id = None

    if opts.get('existing'):
        request_id = opts.get('existing')

    fields = {}

    c = repo.changectx(rev)
    if parent is None:
        parent = repo[0]
    changesets_string = get_changesets_string(repo, parent, c)

    # Don't clobber the summary and description for an existing request
    # unless specifically asked for
    if opts.get('update') or not request_id:
        fields['summary']       = to_utf_8(c.description().splitlines()[0], encoding)
        fields['description']   = to_utf_8(changesets_string, encoding)
        fields['branch']        = to_utf_8(c.branch(), encoding)

    if opts.get('summary'):
        fields['summary'] = to_utf_8(opts.get('summary'), encoding)

    if opts.get('working_directory'):
        fields['summary'] = to_utf_8('(gist) ', encoding) + fields['summary']

    if opts.get('description'):
        fields['description'] = to_utf_8(opts.get('description'), encoding)

    diff = getdiff(ui, repo, c, parent, opts)
    ui.debug('\n=== Diff from parent to rev ===\n')
    ui.debug(diff + '\n')

    if rparent and parent != rparent:
        parentdiff = getdiff(ui, repo, parent, rparent, opts)
        ui.debug('\n=== Diff from rparent to parent ===\n')
        ui.debug(parentdiff + '\n')
    else:
        parentdiff = ''

    if opts.get('update') or not request_id:
        for field in ('target_groups', 'target_people', 'bugs_closed'):
            if opts.get(field):
                value = ','.join(opts.get(field))
            else:
                value = ui.config('reviewboard', field)
            if value:
                fields[field] = to_utf_8(value, encoding)

    ui.status('\n%s\n' % changesets_string)
    ui.status('reviewboard:\t%s\n' % server)
    ui.status('\n')
    username = opts.get('username') or ui.config('reviewboard', 'user')
    if username:
        ui.status('username: %s\n' % username)
    password = opts.get('password') or ui.config('reviewboard', 'password')
    if password:
        ui.status('password: %s\n' % '**********')

    try:
        reviewboard = make_rbclient(server, username, password, proxy=proxy,
                                    apiver=opts.get('apiver'),
                                    trace=opts.get('apitrace'),
                                    disable_ssl_verification=disable_ssl_verification)
    except Exception, e:
        raise util.Abort(_(str(e)))

    if request_id:
        try:
            reviewboard.update_request(request_id, fields=fields, diff=diff,
                parentdiff=parentdiff, publish=opts.get('publish') or
                    not ui.configbool('reviewboard', 'explicit_publish_update'))
        except ReviewBoardError, msg:
            raise util.Abort(_(str(msg)))
    else:
        repo_id = None
        repo_name = None
        submit_as = opts.get('submit_as')

        if repo_id_opt:
            try:
                repo_id = str(int(repo_id_opt))
            except ValueError:
                repo_name = repo_id_opt

        if not repo_id:
            try:
                repositories = reviewboard.repositories()
            except ReviewBoardError, msg:
                raise util.Abort(_(str(msg)))

            if not repositories:
                raise util.Abort(_('no repositories configured at %s' % server))

            if repo_name:
                repo_dict = dict((r.name, r.id) for r in repositories)
                if repo_name in repo_dict:
                    repo_id = repo_dict[repo_id_opt]
                else:
                    raise util.Abort(_('invalid repository name: %s') % repo_name)
            else:
                ui.status('Repositories:\n')
                repo_ids = set()
                for r in repositories:
                    ui.status('[%s] %s\n' % (r.id, r.name) )
                    repo_ids.add(str(r.id))
                if len(repositories) > 1:
                    repo_id = ui.prompt('repository id:', 0)
                    if not repo_id in repo_ids:
                        raise util.Abort(_('invalid repository ID: %s') % repo_id)
                else:
                    repo_id = str(repositories[0].id)
                    ui.status('repository id: %s\n' % repo_id)

        try:
            request_id = reviewboard.new_request(repo_id, fields, diff,
                                                 parentdiff, submit_as)
            if opts.get('publish'):
                reviewboard.publish(request_id)
        except ReviewBoardError, msg:
            raise util.Abort(_(str(msg)))

    request_url = '%s/%s/%s/' % (server, "r", request_id)

    if not request_url.startswith('http'):
        request_url = 'http://%s' % request_url

    msg = 'review request draft saved: %s\n'
    if opts.get('publish'):
        msg = 'review request published: %s\n'
    ui.status(msg % request_url)

    if opts.get('webbrowser') or \
        ui.configbool('reviewboard', 'launch_webbrowser'):
        launch_browser(ui, request_url)

def remoteparent(ui, repo, opts, rev, upstream=None):
    if upstream:
        remotepath = ui.expandpath(upstream)
    else:
        remotepath = ui.expandpath(ui.expandpath('reviewboard', 'default-push'),
                                   'default')
    remoterepo = hg.peer(repo, opts, remotepath)
    out = findoutgoing(repo, remoterepo)
    ancestors = repo.changelog.ancestors([repo.changelog.rev(repo.lookup(rev))])
    for o in out:
        orev = repo[o]
        a, b, c = repo.changelog.nodesbetween([orev.node()], [repo[rev].node()])
        if a:
            return orev.parents()[0]

def findoutgoing(repo, remoterepo):
    # The method for doing this has changed a few times...
    try:
        from mercurial import discovery
    except ImportError:
        # Must be earlier than 1.6
        return repo.findoutgoing(remoterepo)

    try:
        if HgVersion(util.version()) >= HgVersion('2.1'):
            outgoing = discovery.findcommonoutgoing(repo, remoterepo)
            return outgoing.missing
        common, outheads = discovery.findcommonoutgoing(repo, remoterepo)
        return repo.changelog.findmissing(common=common, heads=outheads)
    except AttributeError:
        # Must be earlier than 1.9
        return discovery.findoutgoing(repo, remoterepo)

def launch_browser(ui, request_url):
    # not all python installations have the webbrowser module
    from mercurial import demandimport
    demandimport.disable()
    try:
        import webbrowser
        webbrowser.open(request_url)
    except:
        ui.status('unable to launch browser - webbrowser module not available.')

    demandimport.enable()

def to_utf_8(s, encoding):
    if encoding:
        return s.decode(encoding, 'replace').encode('utf-8')
    else:
        return s

def get_changesets_string(repo, parentctx, ctx):
    """Build a summary from all changesets included in this review."""
    contexts = []
    for node in repo.changelog.nodesbetween([parentctx.node()],[ctx.node()])[0]:
        currctx = repo[node]
        if node == parentctx.node():
            continue

        contexts.append(currctx)

    if len(contexts) == 0:
        contexts.append(ctx)

    contexts.reverse()

    changesets_string = '* * *\n\n'.join(
                ['Changeset %s:%s\n---------------------------\n%s\n' %
                (ctx.rev(), ctx, ctx.description())
                for ctx in contexts])

    return changesets_string
