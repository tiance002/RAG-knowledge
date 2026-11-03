"""Build an inspectable, source-linked conversational rewrite draft; no models."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'var/dynamic-routing-20261101'
REWRITES = {
 'a-followup-019':'在同一数字表面模型格网大小编码规则中，“05”表示什么？',
 'a-followup-021':'金华市2010年的人均生产总值是多少？',
 'a-followup-035':'HJ 1134—2020 技术规范的发布日期与实施日期是否相同？请分别列出两个日期。',
 'followup-b-55':'In Pew Research Center’s November 21, 2016 report “Low Marks for Major Players in 2016 Election – Including the Winner”, what were the unweighted sample size and plus-or-minus sampling error for Clinton voters?',
 'followup-b-56':'In Pew Research Center’s November 21, 2016 report “Low Marks for Major Players in 2016 Election – Including the Winner”, what were the unweighted sample size and plus-or-minus sampling error for Trump voters?',
 'followup-b-59':'国务院办公厅2024年度《收入支出决算总表》中，年初结转和结余是多少万元？',
 'followup-b-60':'国务院办公厅2024年度《收入支出决算总表》中，本年收入合计是多少万元？',
 'followup-b-75':'COSTCO FY2021资产负债表中，流动负债合计是多少百万美元？',
 'followup-b-78':'COSTCO FY2021资产负债表中，包含非控制性权益的总权益是多少百万美元？',
 'followup-b-79':'COSTCO在2021年运营的815个会员仓库，其统计日期是什么？',
 'followup-b-80':'Nike FY2021资产负债表中的应计负债是多少百万美元？',
 'followup-c:001':'In transportation report 4d9932b0c771, what is the total cost to date for the Stormwater Testing and Maintainability Center project?',
 'followup-c:002':'In transportation report 5bdd15609060, what percentage of the NOAA Atlas 14, Vol. 13 project work was completed to date?',
 'followup-c:003':'In transportation report 763f29f662b1, what was the total cost to date for the Support Services for Peer Exchanges project?',
 'followup-c:004':'In transportation report bd20413d8669, what percentage of work was completed to date for the Support Services for Peer Exchanges project?',
 'followup-c:005':'In transportation report 408f7757b382, what was the 6PPD-Quinone stormwater management project number?',
 'followup-c:006':'In table T01, fiscal year 2018, how many public libraries were recorded for Alaska?',
 'followup-c:007':'In table T07, fiscal year 2018, what was Alaska’s total public library operating revenue, in thousands?',
 'followup-c:008':'What percentage of all corn planted in Illinois was insect-resistant Bt only in 2001?',
 'followup-c:009':'In the 2022 apple price table, what was the average price per cup equivalent for fresh apples?',
 'followup-c:010':'In the 2022 avocado price table, what was the preparation yield factor for fresh avocados?',
 'followup-c:011':'In transportation report 8ba77f7722c2, what is the total cost to date for the liquefaction hazards Phase 1 project?',
 'followup-c:012':'In the 2022 avocado price table, what is the average price per cup equivalent for fresh avocados?',
 'followup-c:013':'In the 2022 apple price table, what is the average retail price per pint for ready-to-drink apple juice?',
 'followup-c:014':'Who is the project manager for transportation report 408f7757b382 on 6PPD-Quinone stormwater management?',
 'followup-c:015':'When did the NOAA Atlas 14, Vol. 13 project in transportation report 5bdd15609060 start?',
 'followup-c:016':'What is the current project end date of transportation project TPF-5(475) in report 5bdd15609060?',
 'followup-c:017':'In table T01, fiscal year 2018, what is the population of Alabama’s library legal service area, in thousands?',
 'followup-c:018':'In table T07, fiscal year 2018, what is Alabama’s local public library operating revenue, in thousands?',
 'followup-c:019':'In the 2022 apple price table, what is the preparation yield factor for fresh apples?',
 'a-diff-019':'数字表面模型文件命名的格网大小编码中，“05”表示多少米，比“10”表示的格网小多少米？',
 'a-diff-021':'根据金华市2010年的有关材料，2005年人均生产总值是多少元？请写出计算式。',
 'a-diff-024':'根据龙岩市政府工作报告中2005年的数据，转移就业人数比农村劳动力技能培训人数多多少万人？两者一定是同一批人吗？',
 'a-diff-007':'若UTC时间是周一23:30，换算为北京时间是周几几点？',
 'a-image-diff-001':'在RAPTOR论文 Figure 1 中，绿色和黄色的多簇节点分别是什么编号？按编号升序列出它们各自所在簇的节点集合。',
 'a-image-diff-002':'在RAPTOR论文 Figure 4 中，排除两题共同检索节点后，RAPTOR只为问题1和只为问题2检索了哪些节点？各自按升序列出。',
 'fd-b-01':'In Pew Research Center’s November 21, 2016 report, for men and women separately subtract No from Yes on electing a woman president in their lifetime. Which gap is larger, by how many percentage points, and what does the report say about statistical significance?',
 'fd-b-03':'In Pew Research Center’s November 21, 2016 report, compare Clinton and Trump voters’ Yes shares on electing a woman president in their lifetime and their sampling-error margins. Give both differences in percentage points and state whether these two margins alone establish statistical significance.',
}

def read(name):
    return [json.loads(line) for line in (ROOT/name).read_text(encoding='utf8').splitlines() if line.strip()]

def build():
    a=[x for x in read('followup-a.jsonl') if x['candidate_id']=='a-followup-035']
    b=read('followup-b.jsonl')
    excluded={'followup-c:001','followup-c:002','followup-c:004','followup-c:007','followup-c:008',
              'followup-c:012','followup-c:015','followup-c:017','followup-c:018','followup-c:019'}
    c=[x for x in read('followup-c.jsonl') if x['candidate_id'] not in excluded]
    hard=read('followup-difficulty-a.jsonl')+read('followup-difficulty-c.jsonl')+read('followup-difficulty-image.jsonl')
    hard += [x for x in read('followup-difficulty-b.jsonl') if x['qid'] in ('fd-b-01','fd-b-03')]
    result=[]
    for item in a+b+c+hard:
        identifier=item.get('candidate_id', item.get('qid'))
        original_previous=item.get('previous_question', item.get('prior_question'))
        previous=item.get('prior_question_with_document') or original_previous
        previous_answer=item.get('previous_answer_reference',item.get('previous_answer', item.get('prior_answer')))
        followup=item['followup_question']
        if identifier=='a-followup-019':
            previous='数字表面模型文件命名中，“10”在格网大小中代表什么意思？'
        if identifier=='a-followup-035':
            previous='HJ 1134—2020 这份技术规范的实施日期是什么？'
            followup='这份规范的发布日期与实施日期相同吗？请分别列出两个日期。'
        if identifier=='a-diff-019':
            previous='数字表面模型文件命名中，“10”在格网大小中代表什么意思？'
        if identifier=='a-diff-024':
            previous='2005年龙岩市农村劳动力技能培训了多少人？'
        if identifier=='a-diff-007':
            followup='若 UTC 时间是周一23:30，换算后是周几几点？'
        if identifier.startswith('a-image-diff-'):
            previous='In the RAPTOR paper, '+previous[0].lower()+previous[1:]
        if identifier in {'fd-b-01','fd-b-03'}:
            previous='In Pew Research Center’s November 21, 2016 report “Low Marks for Major Players in 2016 Election – Including the Winner”, '+previous[0].lower()+previous[1:]
        if identifier=='fd-b-03':
            followup='For Trump and Clinton voters in that report, compare their Yes percentages on electing a woman president in their lifetime and their reported sampling-error margins. Give both differences in percentage points, and state whether these two margins alone establish that the Yes difference is statistically significant.'
        if identifier in {'followup-c:006','followup-c:007','followup-c:017','followup-c:018'}:
            previous += ' Use fiscal year 2018.'
        if identifier in {'followup-c:009','followup-c:010','followup-c:012','followup-c:013','followup-c:019'}:
            previous += ' Use the 2022 price table.'
        if identifier=='followup-b-78':
            followup='同一张表中，包括非控制性权益在内的总权益是多少？'
        if identifier=='followup-c:004':
            followup="How much of this project's work had been completed?"
        source=item.get('gold_evidence') or item.get('evidence_refs') or ([item['source']] if item.get('source') else None)
        if identifier=='fd-b-03':
            source=[dict(x) for x in source]
            source[0]['exact_visual_text_or_cells'] += ['Trump voters','17','78','Clinton voters','17','81']
        answer=item.get('gold_answer', item.get('expected_answer'))
        rewrite=REWRITES.get(identifier) or item.get('expected_standalone_rewrite')
        if identifier=='difficulty-c:004':
            rewrite='For Alabama in fiscal year 2018, how much of the public-library operating revenue in table T07 came from sources other than local government, in thousands? Show the calculation.'
        assert rewrite and previous and previous_answer and item.get('followup_question') and answer is not None and source, identifier
        parent=item.get('previous_qid',item.get('source_qid',item.get('parent_qid')))
        if not parent:
            parent={'difficulty-c:001':'office50:002','difficulty-c:002':'office50:025',
                    'difficulty-c:003':'office50:036','difficulty-c:004':'office50:026'}.get(identifier)
        result.append({'id':identifier,'previous_qid':parent,
                       'previous_question':previous,'previous_answer_reference':previous_answer,
                       'previous_question_refined_for_scope':previous != original_previous,
                       'previous_answer_provenance':'source-grounded reference; synthetic test history, not a recorded live reply',
                       'followup_question':followup,'expected_standalone_rewrite':rewrite,
                       'gold_answer':answer,'gold_evidence':source,
                       'previous_answer_evidence':item.get('previous_answer_evidence') or item.get('previous_evidence_refs') or item.get('prior_answer_evidence') or (source if identifier.startswith(('a-followup-','a-diff-')) else None),
                       'comparison_note':'同义表达允许；数值须保留表头、年份和单位；可接受来源显示格式对应的合理四舍五入。',
                       'expected_chunk_ids':[],'chunk_mapping_status':'NOT_RESOLVED',
                       'status':'DRAFT_SOURCE_CHECKED_NOT_EXECUTED',
                       'difficulty_dimensions':(['T','C','J','D','M'] if identifier=='difficulty-c:004' else
                           ['T','C','J','D','M'] if identifier=='fd-b-03' else
                           ['T','C','J','M'] if identifier.startswith(('difficulty-c:','a-image-diff-')) or identifier=='fd-b-01' else
                           ['T','C','J'] if identifier.startswith('a-diff-') else
                           ['C'] if identifier=='a-followup-035' else ['M']),
                       'difficulty_basis':item.get('difficulty_reason',item.get('reason',item.get('difficulty_justification',
                           '按原始表格、列头和来源上下文定位；单项字段题作为基础对照。')))})
    assert len(result)==30 and len({x['id'] for x in result})==30
    for row in result:
        if row['id'] in {'a-diff-021','a-diff-024','difficulty-c:001','difficulty-c:002','difficulty-c:003'}:
            row['difficulty_dimensions'].append('D')
            row['dependency_basis']='需要结合上轮对象及来源中的多个数据项、单元格或字段，不把单个片段命中当作证据完整。'
    # Long context is actual synthetic multi-turn history, not a length label.
    for target in ('a-diff-021','a-diff-024','difficulty-c:002','difficulty-c:004','a-image-diff-002'):
        row=next(x for x in result if x['id']==target)
        history=[]
        for other in result:
            if other['id']==target or other['id'].startswith(('a-image-diff-','a-diff-')): continue
            text=other['previous_question']+' '+str(other['previous_answer_reference'])
            if any(term in text.lower() for term in ('alabama','gold','金华','raptor')): continue
            value=other['previous_answer_reference']
            answer=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
            refs=other.get('previous_answer_evidence') or []
            snippets=[]
            for ref in refs:
                for key in ('source_quote','exact_quote','quote','header_quotes','exact_visual_text_or_cells'):
                    piece=ref.get(key) if isinstance(ref,dict) else None
                    if isinstance(piece,list): piece='；'.join(str(t) for t in piece)
                    if isinstance(piece,str) and piece and piece not in snippets: snippets.append(piece[:600])
            if snippets: answer += '\n来源摘录：'+'；'.join(snippets[:3])
            history.extend([{'role':'user','content':other['previous_question']},
                            {'role':'assistant','content':answer,
                             'source_refs':other['previous_answer_evidence']}])
            if len(history)==30:break
        assert len(history)==30
        value=row['previous_answer_reference']
        final_answer=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
        history.extend([{'role':'user','content':row['previous_question']},
                        {'role':'assistant','content':final_answer,'source_refs':row['previous_answer_evidence']}])
        # Evidence metadata is for reviewers only. Never include source_refs in
        # the model's chat messages, which would leak extra facts and violate
        # provider message schemas.
        row['history_source_map']=[{'message_index':i,'source_refs':m['source_refs']}
                                   for i,m in enumerate(history) if m.get('source_refs')]
        row['history_messages']=[{'role':m['role'],'content':m['content']} for m in history]
        row['history_text_chars']=sum(len(str(x['content'])) for x in history)
        row['history_token_count']='UNKNOWN'
        assert row['history_text_chars'] >= 3000
        row['difficulty_dimensions'].append('L')
        row['history_kind']='synthetic source-grounded reference dialogue, not actual past model replies; send role/content only, never history_source_map or Gold'
    assert set('TCJDLM') <= set().union(*(set(x['difficulty_dimensions']) for x in result))
    return result

if __name__=='__main__':
    items=build(); output=ROOT/'追问30题-候选集.json'
    with output.open('w',encoding='utf8',newline='\n') as f:
        json.dump({'version':'followup30-draft-v1','cases':items},f,ensure_ascii=False,indent=2)
    print(json.dumps({'status':'PASS','cases':len(items),'difficulty_coverage':sorted(set().union(*(set(x['difficulty_dimensions']) for x in items))),
                      'model_calls':0,'test_status':'NOT_RUN'}))
