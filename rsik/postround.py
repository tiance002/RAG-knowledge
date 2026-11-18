"""Advisory-only Curator/Meta over immutable completed-round projections."""
from pathlib import Path
import json

from .contracts import unwrap_response
from .experiment import bytes_hash
from .locking import atomic_write
from .redact import make_visible, project_summary
from .state import canonical


def completed_projection(directories, scanner):
    counts = {'rounds':0, 'proposal_instances':0, 'qualified_proposals':0,
              'abstentions':0, 'adoptions':0, 'validated':0, 'verified':0, 'unknown':0}
    bindings=[]
    for directory in directories:
        path=Path(directory)/'round.json';data=path.read_bytes();result=json.loads(data)
        if result.get('execution_state') not in {'COMPLETED','FAILED'}:
            raise ValueError('UNFINISHED_ROUND')
        counts['rounds']+=1
        counts['proposal_instances']+=len(result['proposal_instances'])
        counts['qualified_proposals']+=len(result['qualified'])
        counts['abstentions']+=sum(v=='ABSTAIN' for v in result['agent_call_results'].values())
        counts['adoptions']+=bool(result.get('adopted_candidate_id'))
        counts['validated']+=sum(d['verdict']=='VALIDATED' for d in result.get('candidate_decisions',[]))
        counts['unknown']+=sum(d['verdict']=='INCONCLUSIVE' and d.get('per_question')!=[] for d in result.get('candidate_decisions',[]))
        bindings.append({'path':str(path.resolve()),'sha256':bytes_hash(data)})
    visible=make_visible([project_summary('REPLAY_SUMMARY',counts,include_count_evidence=True)],
                         parent_code='',scanner=scanner)
    return visible,bindings


def run_advisory(roles, directories, output, *, handbook=None, evidence_context=None):
    """Model text cannot mutate fixtures, quality decisions or deployment.

    Curator deltas may be committed only with a separately constructed trusted
    candidate/source binding context; aggregate ABSTAIN counts have no such refs.
    """
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    visible,bindings=completed_projection(directories,roles.scanner)
    atomic_write(output/'private-bindings.json',canonical(bindings),exclusive=True)
    atomic_write(output/'public-context.json',canonical(visible),exclusive=True)
    result={'curator_status':None,'meta_status':None,'committed_entries':[],
            'evaluation_modified':False,'judgments_modified':False,'production_changed':False}
    for role in ('curator','meta'):
        from .errors import StageFailure
        from .response_audit import NON_STRATEGY_OUTPUT_FAILURES
        try:
            response=roles.call(role,visible)
        except StageFailure as error:
            if error.global_stop or error.reason_code not in NON_STRATEGY_OUTPUT_FAILURES:
                raise
            atomic_write(output/(role+'.json'),canonical({'status':'UNAVAILABLE',
                'reason':error.reason_code}),exclusive=True)
            result[role+'_status']=error.reason_code
            continue
        atomic_write(output/(role+'.json'),canonical(response),exclusive=True)
        body=unwrap_response(response)
        result[role+'_status']=response['status']
        if role=='curator' and body is not None:
            if handbook is None or evidence_context is None:
                result['curator_status']='DELTA_UNCOMMITTED_NO_TRUSTED_CANDIDATE_EVIDENCE'
            else:
                result['committed_entries'].append(handbook.commit_delta(body,evidence_context,idempotency_key=visible['round_id']))
    atomic_write(output/'summary.json',canonical(result),exclusive=True)
    return result
