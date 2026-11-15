# Development-validation candidate, not an autonomous-test gain.
# Extracted selector only; require() is provided by the frozen worker.
def select_member_indices(members, chunks, files, seed_ids, selection, query):
    require(selection in ('all', 'exact_duplicate'), 'invalid selection')
    if not seed_ids:
        return []
    if len(query) < 2:
        return list(range(len(members)))
    seed_members = [member for member in members if member['id'] in seed_ids]
    seed_lengths = [len(member['content']) for member in seed_members]
    longest_seed = max(seed_lengths or [0])
    query_bigrams = [query[index:index + 2] for index in range(max(0, len(query) - 1))]
    scores = []
    for member in members:
        scores.append(len(set([bigram for bigram in query_bigrams if bigram in member['content']])))
    if not any(scores):
        return list(range(len(members)))
    highest_seed_score = max([scores[index] for index, member in enumerate(members) if member['id'] in seed_ids] or [0])
    retained = []
    for index, member in enumerate(members):
        if member['id'] in seed_ids or (scores[index] > 0 and scores[index] + scores[index] >= highest_seed_score):
            retained.append(index)
    return retained
