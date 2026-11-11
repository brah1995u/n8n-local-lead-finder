#!/usr/bin/env python3
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
auth={"httpHeaderAuth":{"id":"cortexInternalAuth","name":"Cortex Internal API"}}
error_id="CortexErrorFlow01"


def http_node(name,path,body=None,retry=True):
    parameters={"method":"POST","url":"http://worker:8000"+path,"authentication":"genericCredentialType","genericAuthType":"httpHeaderAuth","options":{"timeout":240000}}
    if body:
        parameters.update({"sendBody":True,"specifyBody":"json","jsonBody":body})
    return {"id":name,"name":name,"type":"n8n-nodes-base.httpRequest","typeVersion":4.2,"position":[520,240],"parameters":parameters,"credentials":auth,"retryOnFail":retry,"maxTries":2,"waitBetweenTries":5000,"onError":"stopWorkflow"}


def save(name,ident,nodes,connections,error=True):
    workflow={"id":ident,"name":name,"active":False,"nodes":nodes,"connections":connections,"settings":{"executionOrder":"v1","timezone":"Europe/Madrid","executionTimeout":300,"saveDataSuccessExecution":"none","saveDataErrorExecution":"all","saveManualExecutions":True},"pinData":{},"tags":[]}
    if error:
        workflow["settings"]["errorWorkflow"]=error_id
    (ROOT/"workflows"/(ident+".json")).write_text(json.dumps(workflow,ensure_ascii=False,indent=2)+"\n")


trigger={"id":"Error Trigger","name":"Error Trigger","type":"n8n-nodes-base.errorTrigger","typeVersion":1,"position":[280,240],"parameters":{}}
save("Cortex · Помилки",error_id,[trigger,http_node("Записати помилку","/internal/errors",retry=False)],{"Error Trigger":{"main":[[{"node":"Записати помилку","type":"main","index":0}]]}},False)
for stage,label,seconds in [("discovery","Пошук",15),("enrichment","Перевірка",15),("sync","Airtable",60),("maintenance","Обслуговування",300)]:
    interval={"field":"seconds","secondsInterval":seconds} if seconds<60 else {"field":"minutes","minutesInterval":seconds//60}
    start={"id":"Schedule","name":"Розклад","type":"n8n-nodes-base.scheduleTrigger","typeVersion":1.2,"position":[280,240],"parameters":{"rule":{"interval":[interval]}}}
    manual={"id":"Manual","name":"Перевірити вручну","type":"n8n-nodes-base.manualTrigger","typeVersion":1,"position":[280,400],"parameters":{}}
    request=http_node(label,"/internal/tick/"+stage)
    request["parameters"].update({"sendQuery":True,"queryParameters":{"parameters":[{"name":"dry_run","value":"={{ $json.dry_run === true }}"}]}})
    preview={"id":"Preview","name":"Без зовнішніх дій","type":"n8n-nodes-base.set","typeVersion":3.4,"position":[420,400],"parameters":{"assignments":{"assignments":[{"id":"dry","name":"dry_run","value":True,"type":"boolean"}]},"options":{}}}
    save("Cortex · "+label,"Cortex"+stage.title().ljust(10,"0")[:10],[manual,start,preview,request],{"Розклад":{"main":[[{"node":label,"type":"main","index":0}]]},"Перевірити вручну":{"main":[[{"node":"Без зовнішніх дій","type":"main","index":0}]]},"Без зовнішніх дій":{"main":[[{"node":label,"type":"main","index":0}]]}})
cities=json.loads((ROOT/"config/cities.json").read_text())
mapping={c["name"]:c["id"] for c in cities}
trigger={"id":"City Form","name":"Обрати місто","type":"n8n-nodes-base.formTrigger","typeVersion":2.2,"position":[280,240],"parameters":{"formTitle":"Cortex Leads — вибір міста","formDescription":"Прогрес і підключення: http://localhost:5680/","formFields":{"values":[{"fieldLabel":"Місто","fieldType":"dropdown","fieldOptions":{"values":[{"option":c["name"]} for c in cities]},"requiredField":True},{"fieldLabel":"Дія","fieldType":"dropdown","fieldOptions":{"values":[{"option":x} for x in ("Запустити","Продовжити","Повторно перевірити")]},"requiredField":True}]},"options":{"path":"cortex-city","respondWithOptions":{"values":{"formSubmittedText":"Місто додано в чергу. Прогрес: http://localhost:5680/"}}}}}
body='={{ JSON.stringify({city_id: '+json.dumps(mapping,ensure_ascii=False)+'[$json["Місто"]], mode: {"Запустити":"start","Продовжити":"continue","Повторно перевірити":"recheck"}[$json["Дія"]], pilot:true}) }}'
save("Cortex · Керування","CortexControl001",[trigger,http_node("Додати місто","/internal/jobs",body,retry=False)],{"Обрати місто":{"main":[[{"node":"Додати місто","type":"main","index":0}]]}})
print("Generated 6 inactive, credential-free workflow definitions.")
