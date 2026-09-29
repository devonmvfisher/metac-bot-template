"""Counts-only live-run checks, using injectable service boundaries."""
import logging
from . import metadata

logger = logging.getLogger("fbot")


def check_test_posts(questions, state, env, clock, read=metadata.readback):
    groups = {}
    for question in questions.values():
        groups.setdefault(question.post_id, []).append(question)
    found_any, failed = False, not bool(questions)
    for post_id, members in groups.items():
        ids = [q.qid for q in members]
        details = {}
        states = read(post_id, ids, env, sleep=clock.sleep, details=details)
        if any(states.get(qid, ('unknown', 'shape'))[0] == 'unknown' for qid in ids):
            clock.sleep(5)
            retry = read(post_id, ids, env, sleep=clock.sleep, details=details)
            for qid in ids:
                if states.get(qid, ('unknown', 'shape'))[0] == 'unknown':
                    states[qid] = retry.get(qid, ('unknown', 'shape'))
        for q in members:
            status, why = states.get(q.qid, ('unknown', 'shape'))
            posted = 200 <= state.http_status.get(q.qid, 0) < 300
            commented = q.qid in state.commented
            group = bool(len(members) > 1 or details.get('group') or getattr(q, 'is_group', False))
            found_any = found_any or status == 'found'
            label = 'found' if status == 'found' else 'missing' if status == 'none' else 'unknown'
            tolerated = status == 'unknown' and group and posted and commented
            if tolerated:
                label = 'unverified_group'
                logger.info('READBACK_WARNING qid=%s group=1 why=%s', q.qid, why)
            failed = failed or not posted or not commented or (status != 'found' and not tolerated)
            logger.info('POSTED qid=%s http=%s readback=%s comment=%s why=%s group=%s',
                        q.qid, state.http_status.get(q.qid, 0), label,
                        'posted' if commented else 'missing', why, int(group))
    return not failed and found_any


def startup_counts(counts, reasons):
    """The page cap alone is informational; two broken readers are a P0."""
    broken = lambda reason: reason.startswith('http_') or reason in ('shape', 'error')
    return all(value == 'unknown' for value in counts) and all(broken(reason) for reason in reasons)
