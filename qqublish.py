from flask import Flask, render_template
import requests
import subprocess
import shutil
import re
import os
from helpers import make_celery, make_sure_path_exists
import zc.lockfile
from pathlib import Path
from distutils.dir_util import copy_tree

app = Flask(__name__)

app.config.from_pyfile('config.py')

celery = make_celery(app)
github_url = "https://github.com/"

class RepoError(Exception):
    pass


def get_repo_size(username, repo):
    """
    Returns the size of github repo :username/:repo

    :param username:
    :param repo:
    :return: size (in kilobytes)
    """
    git_api_url = "https://api.github.com/repos/{}/{}".format(username,
                                                              repo)

    r = requests.get(git_api_url)
    if not r or r.json().get("message") == "Not found":
        raise RepoError("Repo not found")
    return int(r.json()['size'])

class BookBuilder(object):
    def __init__(self, book_id: str, service: str, base_url: str) -> None:
        self.service = service
        self.base_url = base_url
        self.book_id = book_id

    def repodir(self) -> Path:
        return Path(app.config['JAILPATH']) / self.service / self.book_id

    def lockfile(self) -> Path:
        return self.repodir() / "lockfile"

    def logfile(self) -> Path:
        return self.repodir() / "build.log"

    def clonedir(self) -> Path:
        return self.repodir() / "clone"

    def outputdir(self) -> Path:
        return (Path(app.config['PUBLISHPATH']) / self.service
                / self.book_id)

    def repo_url(self) -> str:
        raise NotImplementedError

    def __str__(self) -> str:
        return f"BookBuilder({self.service}/{self.book_id} " \
               f"-> {self.base_url})"


class GithubBookBuilder(BookBuilder):
    def repo_url(self) -> str:
        return github_url + self.book_id


@app.route('/update/github/<string:username>/<string:repo>/status')
def update_github_status(username, repo):
    pass

@app.route('/update/github/<string:username>/<string:repo>/')
def update_github(username, repo):
    try:
        size = get_repo_size(username, repo)
    except RepoError:
        return render_template("index.html",
                               message="There's no such repo")

    if size > app.config['MAX_SIZE']:
        return render_template("index.html",
                               message="Repo size is too large")

    builder = GithubBookBuilder(book_id=username + "/" + repo,
                                service="github",
                                base_url="http://pub.mathbook.info/github/"
                                         + username + "/" + repo)

    if os.path.isfile(builder.lockfile()):
        return render_template("index.html",
                               message="It's already updating")

    do_update_github.apply_async(args=[builder],
                                 serializer='pickle')

    return render_template("index.html",
                           message="Okay, updating")


@celery.task()
def do_update_github(builder: BookBuilder) -> None:
    """

    :param builder: BookBuilder
    :return:
    """

    make_sure_path_exists(builder.repodir())

    try:
        lock = zc.lockfile.LockFile(builder.lockfile())
    except zc.lockfile.LockError:
        print(f"lockfile exists: {builder}")
        return

    git = app.config['GIT']
    with open(builder.logfile(), "w") as log:
        def log_and_check_output(commands, **kwargs):
            print(" ".join(commands), file=log)
            output = (subprocess.check_output(
                commands, stderr=subprocess.STDOUT,
                **kwargs).decode("utf-8"))
            print(output, file=log)
            log.flush()

        clonedir = builder.clonedir()

        make_sure_path_exists(clonedir)
        try:
            try:
                log_and_check_output([git, "pull"], cwd=clonedir)
            except subprocess.CalledProcessError:
                try:
                    log_and_check_output([git, "clone", builder.repo_url(),
                                          './'], cwd=clonedir)
                except subprocess.CalledProcessError as e:
                    if 'already exists' in e.output:
                        shutil.rmtree(str(clonedir))
                        log_and_check_output([git, "clone", builder.repo_url(),
                                              './'], cwd=clonedir)

            commands = ['sudo', 'docker', 'run', '--rm', '--init',
                        '--net', 'none',
                        '-v', str(clonedir) + ":/home/user/thebook",
                        "ischurov/qqmbr:latest",
                        "build",
                        "--base-url",
                        builder.base_url]

            print("Let's try")
            proc = subprocess.Popen(commands, stdout=log, stderr=log,
                                    cwd=str(clonedir))
            retcode = proc.wait()
            if retcode:
                print("Process terminated with non-zero status", file=log)
        finally:
            print("Removing lock")
            lock.close()
            builder.lockfile().unlink()
    copy_tree(str(clonedir / "build"), str(builder.outputdir()))


if __name__ == '__main__':
    app.run()
