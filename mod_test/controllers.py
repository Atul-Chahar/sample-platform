"""Logic to find all tests, their progress and details of individual test."""

import os
from typing import Any, Dict, List

from flask import (Blueprint, Response, abort, g, jsonify, redirect, request,
                   url_for)
from sqlalchemy import and_

from decorators import template_renderer
from exceptions import TestNotFoundException
from mod_auth.controllers import check_access_rights, login_required
from mod_auth.models import Role
from mod_customized.models import TestFork
from mod_home.models import CCExtractorVersion, GeneralData
from mod_regression.models import (Category, RegressionTestOutput,
                                   regressionTestLinkTable)
from mod_test.models import (Fork, Test, TestPlatform, TestProgress,
                             TestResult, TestResultFile, TestStatus, TestType)
from utility import serve_file_download

mod_test = Blueprint('test', __name__)


@mod_test.before_app_request
def before_app_request() -> None:
    """Curate menu items before app request."""
    g.menu_entries['tests'] = {
        'title': 'Test results',
        'icon': 'flask',
        'route': 'test.index'
    }


@mod_test.errorhandler(TestNotFoundException)
@template_renderer('test/test_not_found.html', 404)
def not_found(error):
    """Show error page when page not found."""
    return {
        'message': error.message
    }


@mod_test.route('/')
@template_renderer()
def index():
    """Show index page for tests."""
    fork = Fork.query.filter(Fork.github.like(f"%/{g.github['repository_owner']}/{g.github['repository']}.git")).first()
    return {
        'tests': Test.query.order_by(Test.id.desc()).limit(50).all(),
        'TestType': TestType,
        'fork': fork
    }


def get_test_results(test) -> List[Dict[str, Any]]:
    """
    Get test results for each category.

    :param test: The test to retrieve the data for.
    :type test: Test
    """
    populated_categories = g.db.query(regressionTestLinkTable.c.category_id).subquery()
    categories = Category.query.filter(Category.id.in_(populated_categories)).order_by(Category.name.asc()).all()
    results = [{
        'category': category,
        'tests': [{
            'test': rt,
            'result': next((r for r in test.results if r.regression_test_id == rt.id), None),
            'files': TestResultFile.query.filter(
                and_(TestResultFile.test_id == test.id, TestResultFile.regression_test_id == rt.id)
            ).all()
        } for rt in category.regression_tests if rt.id in test.get_customized_regressiontests()]
    } for category in categories]
    # Run through the categories to see if they should be marked as failed or passed. A category failed if one or more
    # tests in said category failed.
    for category in results:
        error = False
        for category_test in category['tests']:
            test_error = False
            # A test fails if:
            # - Exit code is not what we expected
            # - There are result files but one of the files is [not identical
            #   and not one of the multiple correct output files]
            # - There are no result files but there should have been
            result = category_test['result']
            if result is not None and result.exit_code != result.expected_rc:
                test_error = True
            if len(category_test['files']) > 0:
                for result_file in category_test['files']:
                    if result_file.got is not None and result.exit_code == 0:
                        file_error = True
                        for file in result_file.regression_test_output.multiple_files:
                            if file.file_hashes == result_file.got:
                                file_error = False
                                break
                        test_error = file_error or test_error
            else:
                # We need to check if the regression test had any file that shouldn't have been ignored.
                outputs = RegressionTestOutput.query.filter(and_(
                    RegressionTestOutput.regression_id == category_test['test'].id,
                    RegressionTestOutput.ignore is False
                )).all()
                got = None
                if len(outputs) > 0:
                    test_error = True
                    got = 'error'
                # Add dummy entry for pass/fail display
                category_test['files'] = [TestResultFile(-1, -1, -1, '', got)]
            # Store test status in error field
            category_test['error'] = test_error
            # Update category error
            error = error or test_error
        category['error'] = error
    results.sort(key=lambda entry: entry['category'].name)
    return results


def get_data_for_test(test, title=None) -> Dict[str, Any]:
    """
    Retrieve the data for a single test, with an optional title.

    :param test: The test to retrieve the data for.
    :type test: Test
    :param title: The title to use in the result. If empty, it's set to 'test {id}'
    :type title: str
    :return: A dictionary with the appropriate values.
    :rtype: dict
    """
    if title is None:
        title = f"test {test.id}"

    # Calculate average runtime for this platform (used when test hasn't started yet)
    avg_minutes = 0
    if len(test.progress) == 0:
        try:
            avg_time_key = 'average_time_' + test.platform.value
            prep_time_key = 'avg_prep_time_' + test.platform.value

            avg_time_record = GeneralData.query.filter(GeneralData.key == avg_time_key).first()
            prep_time_record = GeneralData.query.filter(GeneralData.key == prep_time_key).first()

            avg_duration = float(avg_time_record.value) if avg_time_record else 0
            avg_prep = float(prep_time_record.value) if prep_time_record else 0

            # Total average time in minutes
            avg_minutes = int((avg_duration + avg_prep) / 60)
        except (ValueError, AttributeError):
            avg_minutes = 0

    results = get_test_results(test)

    # Calculate sample progress for initial page load
    completed_samples = len(test.results)
    total_samples = len(test.get_customized_regressiontests())
    progress_percentage = 0
    if total_samples > 0:
        progress_percentage = int((completed_samples / total_samples) * 100)

    return {
        'test': test,
        'TestType': TestType,
        'results': results,
        'title': title,
        'avg_minutes': avg_minutes,
        'sample_progress': {
            'current': completed_samples,
            'total': total_samples,
            'percentage': progress_percentage
        }
    }


def _format_timestamp(timestamp) -> str | None:
    """Format timestamps for the run-manifest PoC."""
    if timestamp is None:
        return None
    return timestamp.strftime('%Y-%m-%d %H:%M:%S (%Z)')


def _result_file_has_diff(result_file: TestResultFile) -> bool:
    """Check if a result file has a meaningful diff."""
    if result_file.got is None:
        return False

    for output_file in result_file.regression_test_output.multiple_files:
        if result_file.got == output_file.file_hashes:
            return False

    return True


def build_run_manifest(test: Test) -> Dict[str, Any]:
    """
    Build a read-only run manifest from the current data model.

    This is a PoC helper. It does not add new tables yet. It proves that the
    existing Test, TestProgress, TestResult, and TestResultFile data can already
    be shaped into one run-centric response.
    """
    from run import config

    progress_entries = [{
        'timestamp': _format_timestamp(entry.timestamp),
        'status': entry.status.value,
        'label': entry.status.description,
        'message': entry.message,
    } for entry in test.progress]

    started_at = test.progress[0].timestamp if len(test.progress) > 0 else None
    finished_at = test.progress[-1].timestamp if test.finished else None

    result_files = TestResultFile.query.filter(TestResultFile.test_id == test.id).order_by(
        TestResultFile.regression_test_id.asc(),
        TestResultFile.regression_test_output_id.asc()
    ).all()
    files_by_regression: Dict[int, List[TestResultFile]] = {}
    for result_file in result_files:
        files_by_regression.setdefault(result_file.regression_test_id, []).append(result_file)

    artifacts = []
    log_file_path = os.path.join(config.get('SAMPLE_REPOSITORY', ''), 'LogFiles', f'{test.id}.txt')
    if os.path.isfile(log_file_path):
        artifacts.append({
            'kind': 'build_log',
            'label': f'Build log for test {test.id}',
            'download_url': url_for('test.download_build_log_file', test_id=test.id),
        })

    regression_entries = []
    passed = 0
    failed = 0

    for result in sorted(test.results, key=lambda entry: entry.regression_test_id):
        files = files_by_regression.get(result.regression_test_id, [])
        diff_files = [result_file for result_file in files if _result_file_has_diff(result_file)]
        status = 'fail' if result.exit_code != result.expected_rc or len(diff_files) > 0 else 'pass'
        if status == 'pass':
            passed += 1
        else:
            failed += 1

        regression_entries.append({
            'regression_test_id': result.regression_test_id,
            'runtime': result.runtime,
            'exit_code': result.exit_code,
            'expected_rc': result.expected_rc,
            'status': status,
            'artifact_count': len(files),
            'diff_count': len(diff_files),
            'diff_download_url': None if len(diff_files) == 0 else url_for(
                'test.generate_diff',
                test_id=test.id,
                regression_test_id=diff_files[0].regression_test_id,
                output_id=diff_files[0].regression_test_output_id,
                to_view=0,
            ),
        })

        for result_file in files:
            artifacts.append({
                'kind': 'result_file',
                'label': (
                    f"Regression {result_file.regression_test_id}, "
                    f"output {result_file.regression_test_output_id}"
                ),
                'regression_test_id': result_file.regression_test_id,
                'output_id': result_file.regression_test_output_id,
                'expected_hash': result_file.expected,
                'got_hash': result_file.got,
                'diff_download_url': None if not _result_file_has_diff(result_file) else url_for(
                    'test.generate_diff',
                    test_id=test.id,
                    regression_test_id=result_file.regression_test_id,
                    output_id=result_file.regression_test_output_id,
                    to_view=0,
                ),
            })

    selected_regression_ids = test.get_customized_regressiontests()

    return {
        'test_id': test.id,
        'title': f'test {test.id}',
        'platform': test.platform.value,
        'platform_label': test.platform.description,
        'test_type': test.test_type.value,
        'test_type_label': test.test_type.description,
        'status': test.progress[-1].status.value if len(test.progress) > 0 else TestStatus.preparation.value,
        'status_label': test.progress[-1].status.description if len(test.progress) > 0 else TestStatus.preparation.description,
        'branch': test.branch,
        'commit': test.commit,
        'pr_nr': test.pr_nr,
        'github_link': test.github_link,
        'repository_url': None if test.fork is None else test.fork.github_url,
        'repository_name': None if test.fork is None else test.fork.github_name,
        'selected_regression_ids': selected_regression_ids,
        'started_at': _format_timestamp(started_at),
        'finished_at': _format_timestamp(finished_at),
        'sample_progress': {
            'current': len(test.results),
            'total': len(selected_regression_ids),
            'percentage': 0 if len(selected_regression_ids) == 0 else int((len(test.results) / len(selected_regression_ids)) * 100),
        },
        'summary': {
            'passed': passed,
            'failed': failed,
            'pending': max(len(selected_regression_ids) - len(test.results), 0),
            'artifacts': len(artifacts),
        },
        'artifacts': artifacts,
        'regressions': regression_entries,
        'progress_events': progress_entries,
        'detail_page_url': url_for('test.by_id', test_id=test.id),
        'json_url': url_for('test.run_manifest_json', test_id=test.id),
    }


@mod_test.route('/get_json_data/<test_id>')
def get_json_data(test_id):
    """
    Retrieve the status of a test id and returns it in JSON format.

    :param test_id: The id of the test to retrieve data for.
    :type test_id: int
    :return: A JSON structure that holds the data about this test.
    :rtype: JSON dict
    """
    test = Test.query.filter(Test.id == test_id).first()
    if test is None:
        g.log.error(f'test with id: {test_id} not found!')
        return jsonify({'status': 'failure', 'error': 'Test not found'})

    pr_data = test.progress_data()
    progress_array = []
    for entry in test.progress:
        progress_array.append({
            'timestamp': entry.timestamp.strftime('%Y-%m-%d %H:%M:%S (%Z)'),
            'status': entry.status.description,
            'message': entry.message
        })

    # Calculate sample progress from existing TestResult data
    completed_samples = len(test.results)
    total_samples = len(test.get_customized_regressiontests())
    progress_percentage = 0
    if total_samples > 0:
        progress_percentage = int((completed_samples / total_samples) * 100)

    return jsonify({
        'status': 'success',
        'details': pr_data["progress"],
        'complete': test.finished,
        'progress_array': progress_array,
        'sample_progress': {
            'current': completed_samples,
            'total': total_samples,
            'percentage': progress_percentage
        }
    })


@mod_test.route('/<test_id>')
@template_renderer()
def by_id(test_id):
    """
    Show specific test.

    :param test_id: id of the test
    :type test_id: int
    :raises TestNotFoundException: when test id is not found
    :return: data for given test id
    :rtype: dict
    """
    test = Test.query.filter(Test.id == test_id).first()
    if test is None:
        g.log.error(f"test with id: {test_id} not found!")
        raise TestNotFoundException(f"Test with id {test_id} does not exist")

    return get_data_for_test(test)


@mod_test.route('/run_manifest/<test_id>')
@template_renderer('test/run_manifest.html')
def run_manifest(test_id):
    """Show a read-only run overview page for the PoC."""
    test = Test.query.filter(Test.id == test_id).first()
    if test is None:
        g.log.error(f"test with id: {test_id} not found!")
        raise TestNotFoundException(f"Test with id {test_id} does not exist")

    return {
        'manifest': build_run_manifest(test),
    }


@mod_test.route('/run_manifest/<test_id>/json')
def run_manifest_json(test_id):
    """Return the PoC run manifest as JSON."""
    test = Test.query.filter(Test.id == test_id).first()
    if test is None:
        g.log.error(f'test with id: {test_id} not found!')
        return jsonify({'status': 'failure', 'error': 'Test not found'})

    return jsonify({
        'status': 'success',
        'manifest': build_run_manifest(test),
    })


@mod_test.route('/ccextractor/<ccx_version>')
@template_renderer('test/by_id.html')
def ccextractor_version(ccx_version):
    """
    Provide tests for a particular version of CCExtractor.

    Look up the hash, find a test for it and redirect.

    :param ccx_version: version of the CCExtractor
    :type ccx_version: str
    :raises TestNotFoundException: when no test is found
    :raises TestNotFoundException: when wrong version is provided
    :return: test data
    :rtype: dict
    """
    version = CCExtractorVersion.query.filter(CCExtractorVersion.version == ccx_version).first()

    if version is not None:
        test = Test.query.filter(Test.commit == version.commit).first()

        if test is None:
            g.log.error(f"test with commit {version.commit} not found!")
            raise TestNotFoundException(f"There are no tests available for CCExtractor version {version.version}")

        return get_data_for_test(test, f"CCExtractor {version.version}")

    raise TestNotFoundException(f"There is no CCExtractor version known as {ccx_version}")


@mod_test.route('/commit/<commit_hash>')
@template_renderer('test/by_id.html')
def by_commit(commit_hash):
    """
    Provide tests for a particular commit of CCExtractor.

    Look up the hash, find a test for it and redirect.

    :param commit_hash: commit hash
    :type commit_hash: str
    :raises TestNotFoundException: when no test is found for the commit
    :return: test data
    :rtype: dict
    """
    test = Test.query.filter(Test.commit == commit_hash).first()

    if test is None:
        g.log.error(f"test with commit hash {commit_hash} not found!")
        raise TestNotFoundException(f"There is no test available for commit {commit_hash}")

    return get_data_for_test(test, f"commit {commit_hash}")


@mod_test.route('/master/<platform>')
@template_renderer('test/by_id.html')
def latest_commit_info(platform):
    """
    Provide tests for the latest commit of CCExtractor for a particular platform.

    :param platform: platform
    :type platform: enum, ["windows", "linux"]
    :raises TestNotFoundException: when no test is found for latest commit
    :return: test data
    :rtype: dict
    """
    try:
        platform = TestPlatform.from_string(platform)
    except ValueError:
        g.log.critical(f"platform {platform} is not supported!")
        abort(404)
    # Look up the hash of the latest commit
    commit_hash = GeneralData.query.filter(GeneralData.key == 'fetch_commit_' + platform.value).first().value
    test = Test.query.filter(Test.commit == commit_hash, Test.platform == platform).first()

    if test is None:
        g.log.error(f"test with commit hash {commit_hash} not found in {str(platform)}!")
        raise TestNotFoundException(f"There is no test available for commit {commit_hash}")

    return get_data_for_test(test, f"master {commit_hash}")


@mod_test.route('/diff/<test_id>/<regression_test_id>/<output_id>', defaults={'to_view': 1})
@mod_test.route('/diff/<test_id>/<regression_test_id>/<output_id>/<int:to_view>')
def generate_diff(test_id: int, regression_test_id: int, output_id: int, to_view: int = 1):
    """
    Generate diff for output and expected result.

    The function is invoked in two modes: to view and to download. In the 'to view' a max of 50 diffs are returned,
    and in 'to download' all the diffs are returned but as a downloadable HTML.

    We check for XHR when the request is to simply view the diff.

    :param test_id: id of the test
    :type test_id: int
    :param regression_test_id: id of the regression test
    :type regression_test_id: int
    :param output_id: id of the generated output
    :type output_id: int
    :param to_view: 1 (default) if test diff to be shown in browser, 0 if to be downloaded
    :type to_view: int
    :return: html diff
    :rtype: html
    """
    from run import config

    result = TestResultFile.query.filter(and_(
        TestResultFile.test_id == test_id,
        TestResultFile.regression_test_id == regression_test_id,
        TestResultFile.regression_test_output_id == output_id
    )).first()

    if result is not None:
        path = os.path.join(config.get('SAMPLE_REPOSITORY', ''), 'TestResults')

        request_xhr_key = request.headers.get('X-Requested-With')
        if (request_xhr_key == 'XMLHttpRequest' or request.accept_mimetypes['application/json']) and to_view == 1:
            return result.generate_html_diff(path)
        elif to_view == 0:
            diff_html_text = result.generate_html_diff(path, to_view=False)
            return Response(
                diff_html_text,
                mimetype='text/html',
                headers={
                    'Content-disposition':
                        f"attachment; filename=test{test_id}_regression{regression_test_id}_output{output_id}.html"
                }
            )
        abort(403, 'generate_diff')

    abort(404)


@mod_test.route('/log-files/<test_id>')
@login_required
def download_build_log_file(test_id):
    """
    Serve download of build log.

    :param test_id: id of the test
    :type test_id: int
    :raises TestNotFoundException: when build log not found
    :raises TestNotFoundException: when test id is not found
    :return: build log text file
    :rtype: Flask response
    """
    from run import config
    test = Test.query.filter(Test.id == test_id).first()

    if test is not None:
        file_name = f"{test_id}.txt"
        log_file_path = os.path.join(config.get('SAMPLE_REPOSITORY', ''), 'LogFiles', file_name)

        if os.path.isfile(log_file_path):
            return serve_file_download(file_name, 'LogFiles')

        raise TestNotFoundException(f"Build log for Test {test_id} not found")

    raise TestNotFoundException(f"Test with id {test_id} not found")


@mod_test.route('/restart_test/<test_id>', methods=['GET', 'POST'])
@login_required
@check_access_rights([Role.admin, Role.tester, Role.contributor])
@template_renderer()
def restart_test(test_id):
    """
    Admin or Test User can restart the running or finished test.

    :param test_id: Test ID of the test which user want to restart
    :type test_id: int
    """
    test = Test.query.filter(Test.id == test_id).first()
    test_fork = TestFork.query.filter(TestFork.user_id == g.user.id, TestFork.test_id == test_id).first()
    if not g.user.is_admin and test_fork is None:
        g.log.warning(f"user with id: {g.user.id} tried to access restricted endpoint")
        abort(403)
    TestResultFile.query.filter(TestResultFile.test_id == test.id).delete()
    TestResult.query.filter(TestResult.test_id == test.id).delete()
    TestProgress.query.filter(TestProgress.test_id == test.id).delete()
    g.db.commit()
    g.log.info(f"test with id: {test_id} restarted")
    return redirect(url_for('.by_id', test_id=test.id))


@mod_test.route('/stop_test/<test_id>', methods=['GET', 'POST'])
@login_required
@check_access_rights([Role.admin, Role.tester, Role.contributor])
@template_renderer()
def stop_test(test_id):
    """
    Admin or Test User can stop the running test.

    :param test_id: Test ID of the test which user want to stop
    :type test_id: int
    """
    test = Test.query.filter(Test.id == test_id).first()
    test_fork = TestFork.query.filter(TestFork.user_id == g.user.id, TestFork.test_id == test_id).first()
    if not g.user.is_admin and test_fork is None:
        g.log.warning(f"user with id: {g.user.id} tried to access restricted endpoint")
        abort(403)
    message = "Canceled by user"
    if g.user.is_admin:
        message = "Canceled by admin"
    test_progress = TestProgress(test.id, TestStatus.canceled, message)
    g.db.add(test_progress)
    g.db.commit()
    g.log.info(f"test with id: {test_id} stopped")
    return redirect(url_for('.by_id', test_id=test.id))
