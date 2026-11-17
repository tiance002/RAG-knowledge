from app.services.grounded_validation import validate_grounded_answer


def citation(text, chunk_id="chunk-1"):
    return {"chunk_id": chunk_id, "content": text, "fileId": "file-1", "page_number": 10}


def test_exact_source_attribution_and_original_identity():
    sources = [citation("收入为 190395.32 元。支出为 189274.27 元。")]
    original = sources[0].copy()
    result = validate_grounded_answer("收入为 190395.32 元。[1]", sources, finish_reason="stop")
    assert result.status == "VERIFIED"
    assert result.referenced_citation_ids == (1,)
    assert sources[0] == original


def test_swapped_fields_are_unsupported_even_if_values_occur():
    sources = [citation("收入为 190395.32 元。支出为 189274.27 元。")]
    result = validate_grounded_answer("收入为 189274.27 元。[1]", sources, finish_reason="stop")
    assert result.status == "UNSUPPORTED"


def test_facts_must_be_in_the_cited_source():
    sources = [citation("收入为 10 元。"), citation("支出为 20 元。", "chunk-2")]
    result = validate_grounded_answer("收入为 20 元。[1]", sources, finish_reason="stop")
    assert result.status == "UNSUPPORTED"


def test_unsupported_date_or_identifier():
    sources = [citation("会议于 2024-01-01 举行，编号 A-123。")]
    for answer in ("会议于 2025-01-01 举行。[1]", "编号 B-123。[1]"):
        assert validate_grounded_answer(answer, sources, finish_reason="stop").status == "UNSUPPORTED"


def test_false_substring_and_uncited_facts_do_not_verify():
    sources = [citation("该结果不是 42。")]
    assert validate_grounded_answer("42。[1]", sources, finish_reason="stop").status == "UNKNOWN"
    assert validate_grounded_answer("该结果不是 42。[1] 因此结果是 42。", sources, finish_reason="stop").status == "UNKNOWN"


def test_punctuation_after_citation_is_not_an_uncited_claim():
    sources = [citation("收入为 10 元。")]
    assert validate_grounded_answer("收入为 10 元 [1].", sources, finish_reason="stop").status == "VERIFIED"
    assert validate_grounded_answer("收入为 10 元 [1]. 另有 20 元", sources, finish_reason="stop").status == "UNKNOWN"


def test_exact_equals_row_keeps_field_value_and_unit_bound():
    source = citation("Infrared Radiation=Travels to the centre of the Food | Microwaves=Sent 1 cminto the food")
    answer = "According to the table, microwaves are sent 1 cm into the food [1]."
    assert validate_grounded_answer(answer, [source], finish_reason="stop").status == "VERIFIED"
    for incorrect in (
        "According to the table, infrared radiation is sent 1 cm into the food [1].",
        "According to the table, microwaves are sent 2 cm into the food [1].",
        "According to the table, microwaves are sent 1 mm into the food [1].",
        "According to the table, microwaves are not sent 1 cm into the food [1].",
    ):
        assert validate_grounded_answer(incorrect, [source], finish_reason="stop").status != "VERIFIED"
    qualified = citation("Microwaves=Sent 1 cminto the food | Note=This claim is false")
    assert validate_grounded_answer(answer, [qualified], finish_reason="stop").status != "VERIFIED"


def test_single_row_table_requires_exact_header_and_value():
    source = citation('| Budget | Percentage of Work Completed to Date |\n|:---|:---|\n| $7 | 7% |')
    assert validate_grounded_answer('The Percentage of Work Completed to Date is 7% [1].', [source], finish_reason='stop').status == 'VERIFIED'
    assert validate_grounded_answer('The percentage of work completed to date is 7%. [1]', [source], finish_reason='stop').status == 'VERIFIED'
    assert validate_grounded_answer('Percentage of Work Completed to Date: 7% [1]', [source], finish_reason='stop').status == 'VERIFIED'
    for answer in ('The Budget is 7% [1].', 'The Percentage of Work Completed to Date is 7 dollars [1].',
                   'The Percentage of Work Completed to Date is not 7% [1].'):
        assert validate_grounded_answer(answer, [source], finish_reason='stop').status != 'VERIFIED'
    extra_row = citation(source['content'] + '\n| $9 | 8% |')
    assert validate_grounded_answer('The Percentage of Work Completed to Date is 7% [1].', [extra_row], finish_reason='stop').status != 'VERIFIED'


def test_explicit_paired_fields_require_both_cited_sources():
    question = '住房保障支出在总收支表与财政拨款收支表中分别是多少万元？'
    sources = [citation('总收支表：14,009.54万元'), citation('财政拨款收支表：13,898.15万元', 'chunk-2')]
    one = validate_grounded_answer('总收支表为14,009.54万元。[1]', sources, finish_reason='stop', question=question)
    both = validate_grounded_answer('总收支表为14,009.54万元。[1]财政拨款收支表为13,898.15万元。[2]', sources, finish_reason='stop', question=question)
    assert one.status != 'VERIFIED'
    assert one.missing_fields == ('财政拨款收支表',)
    assert both.status == 'VERIFIED'


def test_invalid_empty_truncated_and_citations():
    sources = [citation("事实。")]
    for answer, reason in (("", "stop"), ("事实。[1]", "length"), ("事实...[1]...", "stop"), ("事实。[2]", "stop"), ("[1]", "stop")):
        assert validate_grounded_answer(answer, sources, finish_reason=reason).status == "INVALID"
    assert validate_grounded_answer("事实。[1]", [citation("事实。", None)], finish_reason="stop").status == "INVALID"


def test_unknown_completion_and_no_citation():
    sources = [citation("事实。")]
    assert validate_grounded_answer("事实。[1]", sources).status == "UNKNOWN"
    assert validate_grounded_answer("事实。", sources, finish_reason="stop").status == "UNKNOWN"


def test_multiple_markers_and_source_id_preservation():
    sources = [citation("无关。"), citation("事实。", "chunk-2")]
    result = validate_grounded_answer("事实。[1][2]", sources, finish_reason="stop")
    assert result.status == "UNKNOWN"
    assert result.referenced_citation_ids == (1, 2)


def test_source_question_cannot_become_assertion():
    for text in ('收入为42万元', '项目已经完成'):
        sources = [citation(text + '？')]
        assert validate_grounded_answer(text + '。[1]', sources, finish_reason='stop').status != 'VERIFIED'


def test_direct_synthesis_of_distinct_financial_fields():
    sources = [citation("2023年收入：190395.32万元\n2023年支出：189274.27万元")]
    result = validate_grounded_answer(
        "2023年收入为190395.32万元。[1] 2023年支出为189274.27万元。[1]",
        sources, finish_reason="stop",
    )
    assert result.status == "VERIFIED"
    assert "semantic_relevance_unknown" in result.reasons
    assert "answer_completeness_unknown" in result.reasons


def test_financial_swaps_qualifiers_and_units_are_not_accepted():
    sources = [citation("2023年收入：190395.32万元\n2023年支出：189274.27万元")]
    for claim in ("2023年收入为189274.27万元。[1]", "2023年支出为190395.32万元。[1]",
                  "2023年收入为190395.32元。[1]"):
        assert validate_grounded_answer(claim, sources, finish_reason="stop").status == "UNSUPPORTED"
    assert validate_grounded_answer("2024年收入为190395.32万元。[1]", sources, finish_reason="stop").status == "UNSUPPORTED"
    assert validate_grounded_answer("收入为190395.32万元。[1]", sources, finish_reason="stop").status == "UNKNOWN"


def test_rows_and_citations_are_not_interchangeable():
    sources = [citation("2023年收入：100万元"), citation("2023年支出：200万元", "chunk-2")]
    answer = "2023年收入为100万元。[1] 2023年支出为200万元。[2]"
    assert validate_grounded_answer(answer, sources, finish_reason="stop").status == "VERIFIED"
    assert validate_grounded_answer(answer.replace("[2]", "[1]"), sources, finish_reason="stop").status == "UNSUPPORTED"
    repeated = "2023年收入为100万元。[1] 2023年收入为100万元。[1]"
    assert validate_grounded_answer(repeated, sources, finish_reason="stop").status == "UNKNOWN"


def test_neutral_quotation_wrapper_and_unsupported_wrapper():
    sources = [citation("该项目已完成。")]
    assert validate_grounded_answer("原文指出：“该项目已完成。”[1]", sources, finish_reason="stop").status == "VERIFIED"
    assert validate_grounded_answer("这说明项目成功：“该项目已完成。”[1]", sources, finish_reason="stop").status == "UNKNOWN"


def test_cross_line_context_cannot_be_dropped():
    for source, answer in (
        ("以下说法是错误的：\n收入为42万元。", "收入为42万元。[1]"),
        ("2023年财务数据\n收入：42万元", "收入为42万元。[1]"),
        ("未经证实的传言：\n项目已完成。", "项目已完成。[1]"),
        ("以下说法是错误的。收入为42万元。", "收入为42万元。[1]"),
        ("未经证实的传言：\n项目已完成。", "原文指出：“项目已完成。”[1]"),
    ):
        result = validate_grounded_answer(answer, [citation(source)], finish_reason="stop")
        assert result.status == "UNKNOWN"
        assert "source_context_unverified" in result.reasons


def test_question_in_context_cannot_be_discarded():
    source = "是否存在争议？\n收入：42万元"
    assert validate_grounded_answer("收入为42万元。[1]", [citation(source)], finish_reason="stop").status == "UNKNOWN"
