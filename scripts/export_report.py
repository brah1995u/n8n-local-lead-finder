#!/usr/bin/env python3
"""Save the local coverage report without contacting any upstream service."""
import argparse
import json
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parent.parent
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('city',default='gandia',nargs='?')
    args=parser.parse_args()
    with urlopen('http://localhost:5680/api/coverage/'+quote(args.city,safe='')) as response:data=json.load(response)
    stem='coverage-'+''.join(x for x in args.city if x.isalnum() or x=='-')
    (ROOT/'runtime'/f'{stem}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    lines=[f"# {data['city']}: покриття",'',f"Стан: {data['job_status']}. Оновлено: {data['generated_at']}",'',f"Кандидатів: {data['totals']['candidates']}; перевірено: {data['totals']['checked']}; готові A/B: {data['totals']['ready']}; на перевірку: {data['totals']['review']}; у черзі: {data['totals']['queued']}.",'',data.get('reason') or '', '', '| Прохід | Стан | Результатів | USD |','|---|---|---|---|']
    for p in data['passes']:
        if p['kind']!='serp':lines.append(f"| {p['zone'] or p['key']} | {p['status']} | {p['result_count']} | {p['cost_usd']} |")
    lines+=['','## Незавершена робота','']
    for p in data['passes']:
        if p['status'] not in ('done','superseded'):lines.append(f"- {p['key']}: {p['status']}. {p['reason'] or ''}")
    for p in data['public_lists']:
        lines.append(f"- Список {p['url']}: {p['status']}. {p['reason'] or ''}")
    if data.get('queued_candidates'):
        lines+=['','## Кандидати у залишковій черзі','']
        for p in data['queued_candidates']:lines.append(f"- {p['name']}: {p['phone'] or 'Телефон не знайдено'} · {p['reason']}")
    lines+=['',data['limitation']]
    (ROOT/'runtime'/f'{stem}.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'report':f'runtime/{stem}.md','status':data['job_status'],'totals':data['totals']},ensure_ascii=False))
