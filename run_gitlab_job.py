import boto3
import io
import logging
import os
import requests
import xunitparser
import zipfile

from contextlib import contextmanager
from time import sleep


PROJECT_ID = os.getenv("PROJECT_ID", 851)
REF = os.getenv("TEST_BRANCH", "master")
ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")
PRIVATE_TOKEN = os.getenv("PRIVATE_TOKEN")
assert PRIVATE_TOKEN, "Not found variable PRIVATE_TOKEN"


def get_slack_webhook():
    profile = f"plwp-{ENVIRONMENT}"
    try:
        logger.info(f"Using profile: {profile}")
        session = boto3.Session(region_name='us-east-1', profile_name=profile)
        ssm = session.client('ssm')
        result = ssm.get_parameter(Name=f'/plwp/{ENVIRONMENT}/slack/webhook',
                                   WithDecryption=True)
        return result['Parameter']['Value']
    except Exception as e:
        logger.error(f"Failed to get slack webhook from ssm:\n{e}")
    w_hook = os.getenv("SLACK_WEB_HOOK", None)
    if w_hook:
        logger.info("Found web-hook in env variables")
        return w_hook
    return os.getenv("SLACK_WEB_HOOK", None)


def configure_logger():
    logger = logging.getLogger('patrones-tests')
    _format = '%(asctime)s - %(levelname)s - %(message)s'
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_format)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger


@contextmanager
def log_level(log_level):
    old_level = logger.level
    try:
        logger.setLevel(log_level)
        yield logger
    finally:
        logger.setLevel(old_level)


logger = configure_logger()


class SessionRequest:

    def __init__(self, project_id, private_token):
        self._session = requests.Session()
        self._session.headers.update({"Private-Token": private_token,
                                      "Content-Type": "application/json"})
        self._base_api_url = "https://{repo_url}/api/v4/projects/%s" % project_id

    def get_full_url(self, url, request_params=None):
        request_params = (
            request_params if request_params is not None and isinstance(
                request_params, dict) else {})
        return "/".join((self._base_api_url, url.format(**request_params)))

    def post(self, *args, **kwargs):
        return self._request("post", *args, **kwargs)

    def get(self, *args, **kwargs):
        return self._request("get", *args, **kwargs)

    def _request(self, method, url, headers=None, exp_code=200, data=None,
                 params=None, mode="json", **kwargs):
        url = "/".join((self._base_api_url, url))
        try:
            logger.debug(f'Sending {method} request to {url}')
            if data:
                logger.debug(f'Request data: {data}')
            if params:
                logger.debug(f'Request parameters: {params}')
            _response = self._session.request(method, url, json=data,
                                              params=params, **kwargs)
            logger.debug(
                'Got response:\n\tCode: "%s"\n\tText: "%s"\n\tHeaders: %s' % (
                    _response.status_code, _response.text, _response.headers))
        except Exception as e:
            err_msg = (
                'An unexpected error has occured:\n\tMethod: %s\n\t'
                'Url: %s\n\tData: %s\n\tException: %s' %
                (method, url, data, e))
            raise AssertionError(err_msg)
        if _response.status_code != exp_code:
            exc = AssertionError(
                'Response status code: %s. Expected: %s. Text: %s' %
                (_response.status_code, exp_code, _response.text))
            raise exc
        if mode == "json":
            try:
                return _response.json()
            except:
                raise AssertionError(
                    'Failed to convert into json: "%s"' % _response.text)
        elif mode == "content":
            return _response.content
        else:
            return _response.text

    def create_pipeline(self, ref="master", variables=None):
        url = "pipeline"
        data = {"ref": ref}
        if variables:
            data.update({"variables": variables})
        logger.info(f"Starting pipeline on branch {ref} with variables {variables}")
        return self.post(url=url, data=data, exp_code=201)

    def get_pipeline_jobs(self, pipeline_id, scope=["skipped"]):
        """
        :param scope: created, pending, running, failed, success, canceled, skipped, or manual
        """
        url = f"pipelines/{pipeline_id}/jobs"
        params = {
            "scope[]": scope
        }
        return self.get(url=url, params=params)

    def play_job(self, job_id):
        url = f"jobs/{job_id}/play"
        return self.post(url=url)

    def get_job(self, job_id):
        url = f"jobs/{job_id}"
        return self.get(url=url)

    def retry_job(self, job_id):
        url = f"jobs/{job_id}/retry"
        return self.post(url=url, exp_code=201)

    def download_artifact_file(self, job_id, artifact_path):
        url = f"jobs/{job_id}/artifacts/{artifact_path}"
        return self.get(url=url, mode="json")

    def get_job_artifacts(self, job_id, zip_folder=None):
        url = f"jobs/{job_id}/artifacts"
        logger.info("Downloading artifacts")
        with log_level(log_level=logging.INFO):
            content = self.get(url=url, mode="content")
        z = zipfile.ZipFile(io.BytesIO(content))
        z.extractall(path=zip_folder)


def post_results_in_slack(msg, webhook_url):
    headers = {"Content-type": "application/json"}
    resp = requests.post(webhook_url, json=msg, headers=headers)
    if resp.status_code != 200:
        logger.error("Failed to post message")
    else:
        logger.info("Results were sent to slack")


def get_slack_attachments(run_result, env, job_url, failed_tcs):
    colors_map = dict(success="good", failed="danger")
    if failed_tcs:
        text = "Following tests failed"
        failed_tests = [dict(title=r[0], value=r[1]) for r in failed_tcs]
    else:
        text = "All critical tests passed"
        failed_tests = None
    attachment = {
        "color": colors_map.get(run_result, "warning"),
        "pretext": f"Test Run finished on {env} environment",
        "title": "Test Results",
        "title_link": job_url,
        "text": text,
    }
    if failed_tests:
        attachment["fields"] = failed_tests
    return {"attachments": [attachment]}


gitlab_api = SessionRequest(project_id=PROJECT_ID,
                            private_token=PRIVATE_TOKEN)


def start_job(pipeline_id=None, retry=False):
    jobs = gitlab_api.get_pipeline_jobs(pipeline_id=pipeline_id, scope=[])
    job_name = f"{ENVIRONMENT}-environment"
    job = next((j for j in jobs if j["name"] == job_name), None)
    assert job, f"Job {job_name} not found"
    if retry:
        logger.info(f"Retrying job {job['id']}")
        return gitlab_api.retry_job(job_id=job["id"])
    else:
        logger.info(f"Starting job {job['id']}")
        return gitlab_api.play_job(job_id=job["id"])


def wait_job_finished(job_info):
    job_end_statuses = ("failed", "success", "canceled", "skipped")
    while True:
        job_status = job_info["status"]
        logger.info(f"Job status: {job_status}")
        if job_status in job_end_statuses:
            break
        sleep(20)
        job_info = gitlab_api.get_job(job_id=job_info["id"])
    logger.info(f"Job finished with result: {job_status}")
    return job_info


def parse_junit_results(junit_path="logs/junit.xml"):
    try:
        with open(junit_path, "r") as f:
            tr = xunitparser.parse(f)[1]
    except:
        logger.error(f"Failed to parse {junit_path}")
        raise
    return [(f"{f[0].basename}. {f[0].methodname}", f[1]) for f in tr.failures]


def run_ci_job():
    pipeline = gitlab_api.create_pipeline(ref=REF)

    job_info = start_job(pipeline_id=pipeline["id"])
    job_info = wait_job_finished(job_info=job_info)

    gitlab_api.get_job_artifacts(job_id=job_info["id"])

    failures = parse_junit_results()
    if failures:
        logger.error("Following tests failed:\n" +
                     "\n\n".join(f"{f[0]}\n{f[1]}" for f in failures))
    else:
        logger.info("All tests passed")

    slack_hook = get_slack_webhook()
    if slack_hook:
        msg = get_slack_attachments(
            run_result=job_info["status"], env=ENVIRONMENT,
            job_url=job_info["web_url"], failed_tcs=failures)
        post_results_in_slack(msg=msg, webhook_url=slack_hook)
    else:
        logger.warning("Not specified slack webhook url")
    return len(failures)


if __name__ == "__main__":
    exit_code = run_ci_job()
    exit(exit_code)
