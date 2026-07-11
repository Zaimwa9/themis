def test_worker_settings__reviewbot_tasks_registered():
    from book_ia.workers.tasks import WorkerSettings

    names = set()
    for function in WorkerSettings.functions:
        names.add(getattr(function, "name", None) or getattr(function, "__name__", None))

    assert "reviewbot_review_task" in names
    assert "reviewbot_discussion_task" in names


def test_worker_settings__reviewbot_tasks_registration_options():
    from book_ia.workers.tasks import WorkerSettings

    by_name = {}
    for function in WorkerSettings.functions:
        name = getattr(function, "name", None) or getattr(function, "__name__", None)
        by_name[name] = function

    review_fn = by_name["reviewbot_review_task"]
    discussion_fn = by_name["reviewbot_discussion_task"]

    for fn in (review_fn, discussion_fn):
        assert fn.timeout_s == 2700
        assert fn.max_tries == 1
        assert fn.keep_result_s == 0
