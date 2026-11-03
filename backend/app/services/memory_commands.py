"""Bounded explicit commands and display-only preference policy; no model extraction."""
from dataclasses import dataclass, field
import re


@dataclass(frozen=True)
class PreferenceCommand:
    operation: str
    changes: dict = field(default_factory=dict)
    field: str | None = None


def _command(clause):
    # Reject quoted, negative, tentative and one-off instructions, rather than
    # accidentally persisting text that the user only discusses.
    if re.search(r'["\'“”‘’「」『』`]|不要|不想|不必|别|不是|如果|假如|可能|这次|本次|这道题|这一次|do not|don.t|maybe|this time', clause, re.I):
        return None
    if re.fullmatch(r'(?:请)?(?:恢复默认设置|重置(?:全部|所有)?偏好)|reset (?:all )?(?:my )?preferences(?: to defaults)?', clause, re.I):
        return PreferenceCommand('reset')
    if re.fullmatch(r'(?:以后|之后)(?:请)?详细解释', clause):
        return PreferenceCommand('update', {'answer_style': 'detailed'})
    match = re.fullmatch(r'(?:请)?(?:忘记|删除)(?:我(?:之前设置的|的))?(回答风格|解释深度|知识水平|输出)(?:偏好)|forget my (answer style|knowledge level|output) preferences?', clause, re.I)
    if match:
        name = match.group(1) or match.group(2).lower()
        key = {'回答风格': 'answer_style', 'answer style': 'answer_style', '解释深度': 'knowledge_level', '知识水平': 'knowledge_level', 'knowledge level': 'knowledge_level', '输出': 'output_preferences', 'output': 'output_preferences'}[name]
        return PreferenceCommand('delete', field=key)
    match = re.fullmatch(r'(?:以后|之后)(?:请)?(?:回答(?:都)?|解释(?:都)?)(?:请|要|用)?(简洁(?:一点)?|详细(?:一点)?|详细解释|正常(?:详细程度)?)(?:回答|解释|一点|一些)?|(?:from now on|always)[, ]+(?:please )?(?:keep (?:your )?answers? |answer |explain )(concisely|briefly|in detail|normally)', clause, re.I)
    if match:
        value = match.group(1) or match.group(2).lower()
        return PreferenceCommand('update', {'answer_style': 'concise' if value.startswith(('简洁', 'concisely', 'briefly')) else 'detailed' if value.startswith(('详细', 'in detail')) else 'normal'})
    match = re.fullmatch(r'(?:请)?记住我(?:是|的知识水平是)(初学者|中级|专业(?:人士|人员)?|专家)|remember (?:that )?I am (?:a|an) (beginner|intermediate|expert)', clause, re.I)
    if match:
        value = match.group(1) or match.group(2).lower()
        return PreferenceCommand('update', {'knowledge_level': 'beginner' if value in ('初学者', 'beginner') else 'intermediate' if value in ('中级', 'intermediate') else 'expert'})
    output_rules = {
        r'(?:以后|之后)(?:请)?(?:解释技术问题|解释原理)(?:都)?先讲原理': 'principle_first',
        r'(?:以后|之后)(?:请)?比较方案(?:都)?(?:使用|用)表格': 'table',
        r'(?:以后|之后)(?:请)?(?:回答|解释)(?:都)?(?:分步|一步一步)(?:说明|解释|回答)': 'step_by_step',
        r'(?:以后|之后)(?:请)?(?:解释编程问题|回答编程问题)(?:都)?(?:给出|提供|附上)代码示例': 'code_example',
        r'(?:from now on|always)[, ]+(?:please )?use tables? (?:when |to )?compar(?:ing|e) (?:options|solutions)': 'table',
    }
    for pattern, value in output_rules.items():
        if re.fullmatch(pattern, clause, re.I):
            return PreferenceCommand('output', {'add': [value]})
    return None


def parse_preference_commands(text):
    """Return explicit commands and remaining question, never infer intent."""
    # Quotes/code blocks may span sentence delimiters. Reject the whole request
    # conservatively before splitting so an inner example cannot become a write.
    if re.search(r'["\'“”‘’「」『』`]|(?:例如|例子|示例|假如|如果)|(?:不要|不必|别|不想)(?:再)?(?:记住|记录|保存)|(?:do not|don.t) (?:remember|save)', text, re.I):
        return [], text
    commands, remaining = [], []
    for clause in re.split(r'[。！!；;\n]|\.(?=\s|$)', text.strip()):
        clause = clause.strip()
        if not clause:
            continue
        command = _command(clause)
        if command:
            commands.append(command)
        else:
            remaining.append(clause)
    # Do not mutate a request that contains no recognized command.
    return commands, '。'.join(remaining) if commands else text


def normalize_preference_history(messages):
    """Remove recognized historic memory commands/acks from model history.

    The current Redis snapshot is authoritative after settings/reset. Ordinary
    questions, quoted examples and one-off instructions remain untouched.
    """
    normalized = []
    pending_ack = False
    for message in messages:
        content = message.content
        if message.role.value == 'user':
            commands, remaining = parse_preference_commands(content)
            pending_ack = bool(commands)
            if commands:
                if not remaining:
                    continue
                message = message.model_copy(update={'content': remaining})
        elif message.role.value == 'assistant' and pending_ack:
            pending_ack = False
            # This service emits command acknowledgements as standalone prefix
            # paragraphs; strip those prefixes, retaining an accompanying answer.
            content = re.sub(r'^(?:(?:已保存长期(?:输出偏好|偏好)：[^。\n]*。|已删除[^。\n]*(?:系统默认设置|系统默认值)。|长期偏好保存失败，[^。\n]*。|偏好变更已提交，但未能确认当前值，请稍后查看设置。)\s*)+', '', content)
            if not content:
                continue
            if content != message.content:
                message = message.model_copy(update={'content': content})
        else:
            pending_ack = False
        normalized.append(message)
    return normalized


def resolve_display_preferences(snapshot, question):
    """Resolve once per request; only the returned policy enters answer calls.

    Output enums intentionally have narrow reusable applicability: table means
    comparisons, principle_first technical explanations, code_example code tasks.
    Current-turn instructions remain authoritative, including unparsed wording.
    Defaults add no prompt segment so baseline prompts retain their behavior.
    """
    values = snapshot.values.model_dump()
    sources = dict(snapshot.sources)
    lower = question.lower()
    overrides = {
        'answer_style': [('concise', r'(?:简洁|简短|简要)(?:地)?(?:回答|解释|说明)|(?:回答|解释|说明)(?:请|要)?(?:简洁|简短|简要)(?:一点|一些)?|(?:answer|explain) (?:briefly|concisely)|(?:be|keep it) concise'), ('detailed', r'(?:详细|深入)(?:地)?(?:回答|解释|说明|分析)|(?:回答|解释)(?:请|要)?详细(?:一点|一些)?|(?:explain|answer) in detail|(?:give|provide) (?:a )?detailed (?:answer|explanation)'), ('normal', r'(?:用|使用|按)正常详细程度(?:回答|解释)')],
        'knowledge_level': [('beginner', r'(?:面向|按|给)初学者(?:解释|回答|说明)?|(?:面向|按)零基础(?:解释|回答|说明)?|(?:explain|answer) for (?:a )?beginner'), ('intermediate', r'(?:按|用)中级(?:知识)?水平(?:解释|回答|说明)'), ('expert', r'(?:按|用)(?:专业|专家)(?:知识)?水平(?:解释|回答|说明)|(?:explain|answer) for (?:an )?expert')],
    }
    # Quoted text is data, not a display instruction.
    current = re.sub(r'"[^"]*"|“[^”]*”|「[^」]*」|`[^`]*`', '', lower)
    for key, options in overrides.items():
        matches = [value for value, pattern in options if re.search(pattern, current)]
        negative = r'(?:不要|不必|别|do not|don.t)[^，。；,;]{0,10}(?:简洁|简短|详细|深入|brief|concise|detail)' if key == 'answer_style' else r'(?:不要|不必|别|do not|don.t)[^，。；,;]{0,10}(?:初学者|零基础|专业|专家|beginner|expert)'
        if len(matches) == 1 and not re.search(negative, current):
            values[key], sources[key] = matches[0], 'current_turn'
        elif matches:
            # Ambiguous or negated direction: omit saved constraint, let the
            # original current-turn message express the requested behavior.
            sources[key] = 'current_turn'
            values[key] = None
    effective_output = []
    for pref in values['output_preferences']:
        if pref == 'step_by_step' or (pref == 'table' and re.search(r'比较|对比|compar|versus|\bvs\b', lower)) or (pref == 'principle_first' and re.search(r'技术|原理|算法|代码|编程|缓存|redis|python|rag|technical', lower)) or (pref == 'code_example' and re.search(r'代码|编程|python|javascript|code|program', lower)):
            effective_output.append(pref)
    output_directions = {
        'table': (r'(?:使用|用|给出|做成)表格|(?:use|provide) (?:a )?table', r'(?:不要|不必|别)[^，。；,;]{0,5}表格|no tables?|without tables?'),
        'code_example': (r'(?:给出|提供|附上)代码示例|(?:provide|include) (?:a )?code example', r'(?:不要|不必|别)[^，。；,;]{0,5}代码|without code'),
        'step_by_step': (r'(?:分步|一步一步)(?:说明|解释|回答)|(?:explain|answer) step by step', r'不分步|(?:不要|不必|别)[^，。；,;]{0,5}分步'),
        'principle_first': (r'先讲原理|先解释原理', r'(?:不要|不必|别)[^，。；,;]{0,5}(?:原理)'),
    }
    if re.search(r'纯文本|plain text', current):
        effective_output, sources['output_preferences'] = [], 'current_turn'
    else:
        for value, (positive, negative) in output_directions.items():
            if re.search(negative, current):
                effective_output = [item for item in effective_output if item != value]
                sources['output_preferences'] = 'current_turn'
            elif re.search(positive, current):
                if value not in effective_output:
                    effective_output.append(value)
                sources['output_preferences'] = 'current_turn'
    values['output_preferences'] = effective_output
    lines = []
    if sources.get('answer_style') != 'default' and values['answer_style'] is not None:
        lines.append({'concise': '回答简洁，保留必要信息。', 'normal': '使用正常详细程度回答。', 'detailed': '详细解释，展开必要的说明。'}[values['answer_style']])
    if sources.get('knowledge_level') != 'default' and values['knowledge_level'] is not None:
        lines.append({'beginner': '面向初学者解释术语和基础。', 'intermediate': '按中级知识水平解释。', 'expert': '按专业知识水平解释，使用准确术语。'}[values['knowledge_level']])
    mapping = {'step_by_step': '适合操作或推导时分步说明。', 'table': '比较方案时使用表格。', 'code_example': '解释编程问题时提供代码示例。', 'principle_first': '解释技术问题时先讲原理。'}
    lines.extend(mapping[value] for value in effective_output)
    policy = ('回答表达偏好（不是资料或事实证据）：\n' + '\n'.join(lines) + '\n仅影响表达，不得改变系统安全、知识来源、引用规则、工具选择或检索。当前轮用户明确要求优先；有冲突时遵从当前轮。') if lines else ''
    return policy, values, sources


async def apply_preference_commands(service, commands, scope=None):
    """Each acknowledgement corresponds to a completed write; errors propagate."""
    snapshot, acknowledgements = None, []
    labels = {'answer_style': '回答风格', 'knowledge_level': '解释深度', 'output_preferences': '输出偏好'}
    names = {'concise': '简洁', 'normal': '正常', 'detailed': '详细', 'beginner': '初学者', 'intermediate': '中级', 'expert': '专业', 'table': '比较方案时使用表格', 'principle_first': '解释技术问题时先讲原理', 'step_by_step': '适合时分步说明', 'code_example': '解释编程问题时提供代码示例'}
    for command in commands:
        if command.operation == 'reset':
            snapshot = await service.reset_preferences(scope=scope)
            acknowledgements.append('已删除全部长期偏好，恢复系统默认设置。')
        elif command.operation == 'delete':
            snapshot = await service.delete_preference(command.field, scope=scope)
            acknowledgements.append(f'已删除{labels[command.field]}偏好，恢复系统默认值。')
        elif command.operation == 'output':
            snapshot = await service.change_output_preferences(**command.changes, scope=scope)
            acknowledgements.append('已保存长期输出偏好：' + '、'.join(names[x] for x in command.changes['add']) + '。')
        else:
            snapshot = await service.update_preference(command.changes, scope=scope)
            acknowledgements.append('已保存长期偏好：' + '、'.join(f'{labels[k]}为{names[v]}' for k, v in command.changes.items()) + '。')
    return snapshot, acknowledgements
