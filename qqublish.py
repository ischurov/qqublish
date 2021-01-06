from flask import Flask, render_template, jsonify, request, redirect, url_for
import requests
import subprocess
import shutil
import re
import os
from helpers import make_celery, make_sure_path_exists
import zc.lockfile
from pathlib import Path
from distutils.dir_util import copy_tree
from typing import NamedTuple, Optional
from datetime import datetime
from tzlocal import get_localzone

app = Flask(__name__)

app.config.from_pyfile("config.py")

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
    git_api_url = "https://api.github.com/repos/{}/{}".format(username, repo)

    r = requests.get(git_api_url)
    if not r or r.json().get("message") == "Not found":
        raise RepoError("Repo not found")
    return int(r.json()["size"])


BuildStatus = NamedTuple(
    "BuildStatus",
    (
        ("status", str),
        ("log", Optional[str]),
        ("timestamp", Optional[str]),
        ("url", Optional[str]),
    ),
)


class BookBuilder(object):
    def __init__(self, book_id: str, service: str, base_url: str = None) -> None:
        self.service = service
        self.base_url = base_url
        self.book_id = book_id

    def repodir(self) -> Path:
        return Path(app.config["JAILPATH"]) / self.service / self.book_id

    def lockfile(self) -> Path:
        return self.repodir() / "lockfile"

    def logfile(self) -> Path:
        return self.repodir() / "build.log"

    def clonedir(self) -> Path:
        return self.repodir() / "clone"

    def outputdir(self) -> Path:
        return Path(app.config["PUBLISHPATH"]) / self.service / self.book_id

    def repo_url(self) -> str:
        raise NotImplementedError

    def __str__(self) -> str:
        return f"BookBuilder({self.service}/{self.book_id} " f"-> {self.base_url})"

    def status(self) -> BuildStatus:
        tz = get_localzone()

        url = None

        if self.logfile().exists():
            with open(self.logfile()) as file:
                log = file.read()
            timestamp: datetime = tz.localize(
                datetime.fromtimestamp(os.path.getmtime(self.logfile()))
            )
        else:
            log = None
            timestamp = None
        if self.lockfile().exists():
            status = "in-progress"
        else:
            if log and "SUCCESS" in log:
                status = "complete"
                url = self.base_url
            elif log and "FAILED" in log:
                status = "failed"
            else:
                status = "unkown"

        return BuildStatus(
            status, log, timestamp.isoformat() if timestamp else "unknown", url
        )


class GithubBookBuilder(BookBuilder):
    def repo_url(self) -> str:
        return github_url + self.book_id


@app.route("/", methods=["GET", "POST"])
def showform():
    if request.method == "GET":
        return render_template("index.html")
    else:
        url = request.form.get("url")
        m = re.match(r"((https?://)?github.com/)?(\w+)/(\w+)", url)
        if m:
            _, _, username, repo = m.groups()
            return redirect(url_for("update_github", username=username, repo=repo))
        return render_template("index.html", message="Incorrect URL")


@app.route("/update/github/<string:username>/<string:repo>/status/json")
def update_github_status_json(username, repo):
    builder = GithubBookBuilder(
        book_id=username + "/" + repo,
        service="github",
        base_url=app.config["BASEURL_PREFIX_GH"] + username + "/" + repo,
    )
    return jsonify(builder.status()._asdict())


@app.route("/update/github/<string:username>/<string:repo>/status")
def update_github_status(username, repo):
    return render_template("status.html", username=username, repo=repo)


@app.route("/update/github/<string:username>/<string:repo>/")
def update_github(username, repo):
    try:
        size = get_repo_size(username, repo)
    except RepoError:
        return render_template("index.html", message="There's no such repo")

    if size > app.config["MAX_SIZE"]:
        return render_template("index.html", message="Repo size is too large")

    builder = GithubBookBuilder(
        book_id=username + "/" + repo,
        service="github",
        base_url=app.config["BASEURL_PREFIX_GH"] + username + "/" + repo,
    )

    if not builder.lockfile().exists():
        do_update_github.apply_async(args=[builder], serializer="pickle")

    return redirect(url_for("update_github_status", username=username, repo=repo))


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

    git = app.config["GIT"]
    with open(builder.logfile(), "w") as log:

        def log_and_check_output(commands, **kwargs):
            print(" ".join(commands), file=log)
            output = subprocess.check_output(
                commands, stderr=subprocess.STDOUT, **kwargs
            ).decode("utf-8")
            print(output, file=log)
            log.flush()

        clonedir = builder.clonedir()

        make_sure_path_exists(clonedir)
        try:
            try:
                log_and_check_output([git, "pull"], cwd=clonedir)
            except subprocess.CalledProcessError:
                try:
                    log_and_check_output(
                        [git, "clone", builder.repo_url(), "./"], cwd=clonedir
                    )
                except subprocess.CalledProcessError as e:
                    if "already exists" in e.output:
                        shutil.rmtree(str(clonedir))
                        make_sure_path_exists(clonedir)
                        log_and_check_output(
                            [git, "clone", builder.repo_url(), "./"], cwd=clonedir
                        )

            commands = [
                "sudo",
                "docker",
                "run",
                "--rm",
                "--init",
                "--net",
                "none",
                "-v",
                str(clonedir) + ":/home/user/thebook",
                "ischurov/qqmbr:latest",
                "build",
                "--base-url",
                builder.base_url,
            ]

            print("Let's try")
            proc = subprocess.Popen(commands, stdout=log, stderr=log, cwd=str(clonedir))
            retcode = proc.wait()
            if retcode:
                raise Exception("FAILED: Process terminated with " "non-zero status")

            copy_tree(str(clonedir / "build"), str(builder.outputdir()))
            print("SUCCESS", file=log)
        except Exception as e:
            print("FAILED: Something went wrong:", e, file=log)
        finally:
            print("Removing lock")
            lock.close()
            builder.lockfile().unlink()


if __name__ == "__main__":
    app.run()
