from fabric.api import cd, env, hide, prefix, run, settings, show, sudo
from fabric.contrib import console, files
from fabric.utils import abort, fastprint, puts
import os


def clean(app_name):
    if not app_name:
        app_name = raw_input('Name of the application to delete: ')

    if not app_name:
        abort('No application name was provided.')

    puts("Removing app {} from the following machines: {}".format(app_name, ", ".join(env.hosts) if env.hosts else "(None)"))

    if not console.confirm('Are you sure?', default=False):
        abort('Aborting.')

    projects_dir = os.path.join('~', 'projects')
    venvs_dir = os.path.join('~', 'virtualenvs')

    app_dir = os.path.join(projects_dir, app_name)
    venv_dir = os.path.join(venvs_dir, app_name)

    assert _is_subpath(app_dir, projects_dir), "Invalid path '{}'".format(app_dir)
    assert _is_subpath(venv_dir, venvs_dir), "Invalid path '{}'".format(venv_dir)

    if files.exists(app_dir):
        run('rm -rf {}'.format(app_dir))
        if files.exists(projects_dir):
            run('rmdir --ignore-fail-on-non-empty {}'.format(projects_dir))

    if files.exists(venv_dir):
        run('rm -rf {}'.format(venv_dir))
        if files.exists(venvs_dir):
            run('rmdir --ignore-fail-on-non-empty {}'.format(venvs_dir))


def deploy(git_url=None):
    if not git_url:
        git_url = raw_input('Git repo address: ')
    repo_name = _repo_name_from_git_url(git_url)

    puts("Deploying from {} on following machines: {}".format(git_url, ", ".join(env.hosts) if env.hosts else "(None)"))

    if not console.confirm('Are you sure?', default=False):
        abort('Aborting.')

    show_keys = console.confirm('Show deploy keys?', default=False)
    _setup_deploy_keys(show_keys)

    try:
        if show_keys:
            raw_input('Set up the repository with the provided keys and press [Enter] to continue or ctrl+c to abort...')
        else:
            raw_input('By now the deploy keys should be set up on your repo. Press [Enter] to continue or ctrl+c to abort...')
    except KeyboardInterrupt:
        abort('Ctrl+c pressed. Aborting.')

    with hide('output'):
        sudo('apt-get update')

    _setup_app(repo_name, git_url)
    
    if console.confirm('Install PostgreSQL?', default=False):
        _setup_postgres(repo_name)

    if console.confirm('Install Redis?', default=False):
        _setup_redis()

    # Setup the virtual only now, because it is possible some of the earlier requirements (Postgres, Redis) might
    # provide additional system packages.
    _setup_venv(repo_name)

    if console.confirm('Install supervisor?', default=False):
        _setup_supervisor(repo_name)

    if console.confirm('Install nginx?', default=False):
        _setup_nginx(repo_name)


def _setup_deploy_keys(show_keys=False):
    for host in env.hosts:
        with settings(host_string=host):
            if not files.exists(os.path.join('~', '.ssh', 'id_rsa.pub')):
                puts("Didn't find the SSH key on host '%s'. Creating a new one.", host)
                run('ssh-keygen -t rsa -b 4096 -C "devadm@zeppelin.no"')

            if show_keys:
                with show('user'):
                    puts('This is your deploy key for host {}. Use it in your git repository:'.format(host))
                    with hide('running', 'output'):
                        fastprint(run('cat ~/.ssh/id_rsa.pub'), end='\n')


def _setup_app(app_name, git_url):
    with hide('output'):
        sudo('apt-get install -y git python-pip python-dev')

    run('mkdir -p ~/projects')
    with cd('~/projects'):
        if files.exists(app_name):
            with cd(app_name):
                run('git pull')
        else:
            run('git clone {} {}'.format(git_url, app_name))
    
    with hide('output'):
        with cd(os.path.join('~', 'projects', app_name)):
            app_dir = run('pwd')

        sudo('mkdir -p /var/webapps')
        with cd('/var/webapps'):
            if not files.exists(app_name):
                sudo('ln -s {} {}'.format(app_dir, app_name))


def _setup_venv(app_name):
    with hide('output'):
        sudo('apt-get install -y python-virtualenv')
    
    run('mkdir -p ~/virtualenvs')
    with cd('~/virtualenvs'):
        if not files.exists(os.path.join(app_name, 'bin', 'activate')):
            run('virtualenv {}'.format(app_name))

    requirements_file = '~/projects/{}/requirements.txt'.format(app_name)
    if not files.exists(requirements_file):
        if not console.confirm("File {} was not found. Do you want to continue?".format(requirements_file), default=False):
            abort('File {} not found.'.format(requirements_file))

    with prefix('source ~/virtualenvs/{}/bin/activate'.format(app_name)):
        run('pip install -r {}'.format(requirements_file))
        run('pip install gunicorn')

    with hide('output'):
        with cd(os.path.join('~', 'virtualenvs', app_name)):
            venv_dir = run('pwd')

        sudo('mkdir -p /var/virtualenvs')
        with cd('/var/virtualenvs'):
            if not files.exists(app_name):
                sudo('ln -s {} {}'.format(venv_dir, app_name))


def _setup_postgres(db_name):
    create_user = False
    with hide('output'):
        sudo('apt-get install -y postgresql postgresql-contrib libpq-dev')
        if not sudo('psql postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname=\'{}\'"'.format(db_name), user='postgres').strip():
            create_user = True

    with show('output', 'user'):
        sudo('createuser --createdb --no-superuser --no-createrole --pwprompt {}'.format(db_name), user='postgres')
        sudo('createdb {}'.format(db_name), user='postgres')


def _setup_redis():
    with hide('output'):
        sudo('apt-get install -y redis-server')


def _setup_supervisor(app_name):
    with hide('output'):
        sudo('apt-get install -y supervisor')
    
    conf_dir = os.path.join('/', 'etc', 'supervisor', 'conf.d')
    conf_file = os.path.join(conf_dir, '{}.conf'.format(app_name))
    if not files.exists(conf_file):
        files.upload_template(filename='supervisord.conf.sample', 
                              destination=conf_file,
                              template_dir='.',
                              context={'app_name': app_name},
                              backup=False,
                              use_jinja=True,
                              use_sudo=True,)
        sudo('service supervisor restart')


def _setup_nginx(app_name):
    with hide('output'):
        sudo('apt-get install -y nginx')

    nginx_dir = os.path.join('/', 'etc', 'nginx')
    conf_dir = os.path.join(nginx_dir, 'sites-available')
    conf_file = os.path.join(conf_dir, app_name)

    if not files.exists(conf_file):
        files.upload_template(filename='nginx.conf.sample', 
                              destination=conf_file,
                              template_dir='.',
                              context={'app_name': app_name},
                              backup=False,
                              use_jinja=True,
                              use_sudo=True,)
        with cd(os.path.join(nginx_dir, 'sites-enabled')):
            if not files.exists(app_name):
                sudo('ln -s ../sites-available/{app_name} {app_name}'.format(app_name=app_name))
        sudo('service nginx restart')


def _repo_name_from_git_url(git_url):
    git_url = git_url.split('/')[-1]  # from git://github.com:zeppelin-no/repo.git to repo.git
    return git_url.split('.')[0]


def _is_subpath(path, root):
    abs_path = os.path.realpath(path)
    abs_root = os.path.join(os.path.realpath(root), '')

    return abs_path != abs_root and os.path.commonprefix([abs_path, abs_root]) == abs_root
