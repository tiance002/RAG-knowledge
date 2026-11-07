"""One history contract: N previous user turns plus the current question.

System messages are retained. Tool messages belonging to a selected turn remain
with that turn. Apply at a request boundary, never to an executing Agent run.
"""


def message_role(message):
    value = message.get('role') if isinstance(message, dict) else getattr(message, 'role', None)
    value = getattr(value, 'value', value) or getattr(message, 'type', None)
    return {'human': 'user', 'ai': 'assistant'}.get(value, value)


def bound_conversation(messages, previous_rounds):
    rows = list(messages)
    users = [i for i, item in enumerate(rows) if message_role(item) == 'user']
    if not users:
        return rows
    current = users[-1]
    start = users[max(0, len(users) - 1 - max(0, int(previous_rounds)))]
    active = rows[:current + 1]
    # System rules are not conversational turns or user preference memory.
    systems = [item for item in active if message_role(item) == 'system']
    return systems + [item for item in active[start:] if message_role(item) != 'system']


def bound_rewrite_history(history, previous_rounds):
    rows = [item for item in (history or [])
            if message_role(item) in ('user', 'assistant')
            and isinstance(item.get('content') if isinstance(item, dict)
                           else getattr(item, 'content', None), str)]
    current = {'role': 'user', 'content': ''}
    bounded = bound_conversation(rows + [current], previous_rounds)
    return [{'role': message_role(item),
             'content': item.get('content') if isinstance(item, dict) else item.content}
            for item in bounded if item is not current
            and message_role(item) in ('user', 'assistant')]
