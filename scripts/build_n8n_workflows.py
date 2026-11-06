"""Write the importable n8n workflows to workflows/n8n/*.json.

The JSON files are committed; run this after changing a workflow here:

    python scripts/build_n8n_workflows.py

Every workflow talks to Snipmint only through /api/integrations/n8n/* with the "Snipmint API"
credential (Header Auth: X-Snipmint-Secret), and Snipmint's events arrive on Webhook nodes that
check the "Snipmint webhook" credential (same header and secret). Node ids are derived from the
names, so re-running the script gives the same files.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "workflows" / "n8n"
API = "http://snipmint:8000/api/integrations/n8n"  # the Snipmint service in docker-compose.n8n.yml
NS = uuid.UUID("6f1d6a52-6a0e-4a52-9f0e-5e1f00d5a11c")
API_CRED = {"httpHeaderAuth": {"id": "snipmintApiHeader", "name": "Snipmint API"}}
HOOK_CRED = {"httpHeaderAuth": {"id": "snipmintWebhookHeader", "name": "Snipmint webhook"}}


def nid(workflow: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{workflow}/{name}"))


class Flow:
    def __init__(self, name: str, slug: str, description: str):
        self.name, self.slug, self.description = name, slug, description
        self.nodes: list[dict] = []
        self.connections: dict[str, dict] = {}
        self.x = 0

    def add(self, name: str, type_: str, version: float, params: dict, *, pos: tuple[int, int] | None = None,
            **extra) -> str:  # fmt: skip
        node = {"parameters": params, "id": nid(self.slug, name), "name": name, "type": type_,
                "typeVersion": version, "position": list(pos or (self.x, 0)), **extra}  # fmt: skip
        if type_ in ("n8n-nodes-base.webhook", "n8n-nodes-base.wait"):
            node["webhookId"] = nid(self.slug, name + "/hook")
        self.nodes.append(node)
        self.x += 240
        return name

    def link(self, src: str, dst: str, output: int = 0) -> None:
        outs = self.connections.setdefault(src, {"main": []})["main"]
        while len(outs) <= output:
            outs.append([])
        outs[output].append({"node": dst, "type": "main", "index": 0})

    def chain(self, *names: str) -> None:
        for a, b in zip(names, names[1:], strict=False):
            self.link(a, b)

    def dump(self) -> dict:
        return {
            "name": self.name,
            "nodes": self.nodes,
            "connections": self.connections,
            "active": False,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True, "callerPolicy": "workflowsFromSameOwner"},
            "pinData": {},
            "meta": {"templateCredsSetupCompleted": False, "snipmint": {"description": self.description}},
            "tags": [],
        }

    # ------------------------------------------------------------ node helpers

    def webhook(self, name: str, path: str, pos=None) -> str:
        return self.add(name, "n8n-nodes-base.webhook", 2,
                        {"httpMethod": "POST", "path": path, "authentication": "headerAuth",
                         "responseMode": "onReceived", "options": {}},
                        pos=pos, credentials=HOOK_CRED)  # fmt: skip

    def api(self, name: str, method: str, path_expr: str, *, body: str | None = None, idem: str | None = None,
            timeout: int = 30000, keep_going: bool = False, pos=None) -> str:  # fmt: skip
        params: dict = {
            "method": method,
            "url": f"={API}{path_expr}",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "options": {"timeout": timeout},
        }
        if idem:
            params["sendHeaders"] = True
            params["headerParameters"] = {"parameters": [{"name": "Idempotency-Key", "value": idem}]}
        if body is not None:
            params.update({"sendBody": True, "specifyBody": "json", "jsonBody": body})
        extra: dict = {"credentials": API_CRED, "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000}
        if keep_going:
            extra["onError"] = "continueRegularOutput"  # one failure must not stop the others
        return self.add(name, "n8n-nodes-base.httpRequest", 4.2, params, pos=pos, **extra)

    def if_(self, name: str, conditions: list[dict], combinator: str = "and", pos=None) -> str:
        conds = []
        for i, c in enumerate(conditions):
            conds.append({"id": nid(self.slug, f"{name}/{i}"), **c})
        return self.add(name, "n8n-nodes-base.if", 2.2, {
            "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                           "conditions": conds, "combinator": combinator},
            "options": {},
        }, pos=pos)  # fmt: skip

    def wait(self, name: str, amount: str | int, pos=None) -> str:
        return self.add(name, "n8n-nodes-base.wait", 1.1, {"amount": amount, "unit": "seconds"}, pos=pos)

    def code(self, name: str, js: str, pos=None) -> str:
        return self.add(name, "n8n-nodes-base.code", 2, {"jsCode": js}, pos=pos)

    def notifiers(self, src: str, y: int = 0) -> None:
        """Telegram, Discord and e-mail, disabled until you add your own credentials."""
        x = self.x
        for i, (name, type_, ver, params, cred) in enumerate([
            ("Telegram", "n8n-nodes-base.telegram", 1.2,
             {"chatId": "YOUR_CHAT_ID", "text": "={{ $json.message }}", "additionalFields": {}},
             {"telegramApi": {"id": "telegramCred", "name": "Telegram"}}),
            ("Discord", "n8n-nodes-base.discord", 2,
             {"authentication": "webhook", "content": "={{ $json.message }}", "options": {}},
             {"discordWebhookApi": {"id": "discordCred", "name": "Discord webhook"}}),
            ("Email", "n8n-nodes-base.emailSend", 2.1,
             {"fromEmail": "snipmint@example.com", "toEmail": "you@example.com", "subject": "={{ $json.subject }}",
              "emailFormat": "text", "text": "={{ $json.message }}", "options": {}},
             {"smtp": {"id": "smtpCred", "name": "SMTP"}}),
        ]):  # fmt: skip
            self.add(name, type_, ver, params, pos=(x, y + (i - 1) * 180), credentials=cred, disabled=True,
                     onError="continueRegularOutput", notes="Disabled: add your credentials, then enable.")  # fmt: skip
            self.link(src, name)


def report(flow: Flow, name: str, workflow: str, status_expr: str, task_expr: str = "null",
           batch_expr: str = "null", message_expr: str = "''", pos=None) -> str:  # fmt: skip
    body = ("={{ JSON.stringify({ execution_id: String($execution.id) + (" + task_expr + " ? ':' + " + task_expr +
            " : ''), workflow: '" + workflow + "', status: " + status_expr + ", task_id: " + task_expr +
            ", batch_id: " + batch_expr + ", message: String(" + message_expr + ").slice(0, 900) }) }}")  # fmt: skip
    return flow.api(name, "POST", "/executions", body=body, keep_going=True, pos=pos)


EXEC = "JSON.stringify({ execution_id: String($execution.id) })"


def automated_publishing() -> Flow:
    f = Flow("Snipmint: automated publishing", "automated-publishing",
             "A: a clip is ready -> check approval -> publish through Snipmint -> track -> report.")
    f.webhook("Clip ready", "snipmint/clip-ready")
    f.api("Get task", "GET", "/tasks/{{ $json.body.data.task_id }}")
    f.if_("Metadata approved?", [{"leftValue": "={{ $json.approved }}", "rightValue": True,
                                  "operator": {"type": "boolean", "operation": "true", "singleValue": True}}])  # fmt: skip
    f.api("Publish", "POST", "/tasks/{{ $json.id }}/publish", body="={{ " + EXEC + " }}",
          idem="={{ $json.id }}:publish:{{ $json.updated }}", pos=(720, -120))  # fmt: skip
    f.wait("Wait 30 s", 30, pos=(960, -120))
    f.api("Check upload", "GET", "/tasks/{{ $('Publish').item.json.id }}", pos=(1200, -120))
    f.if_("Still uploading?", [
        {"leftValue": "={{ ['PROCESSING', 'UPLOADING'].includes($json.status) }}", "rightValue": True,
         "operator": {"type": "boolean", "operation": "true", "singleValue": True}},
        {"leftValue": "={{ $runIndex }}", "rightValue": 60, "operator": {"type": "number", "operation": "lt"}},
    ], pos=(1440, -120))  # fmt: skip
    report(f, "Report result", "automated-publishing",
           "($json.status === 'PUBLISHED' ? 'success' : ($json.status === 'FAILED' ? 'error' : 'waiting'))",
           task_expr="$json.id", message_expr="$json.label + ($json.video_url ? ' ' + $json.video_url : '') + "
           "($json.error ? ': ' + $json.error : '')", pos=(1680, -40))  # fmt: skip
    f.api("Hold for approval", "POST", "/tasks/{{ $json.id }}/hold", body="={{ " + EXEC + " }}", pos=(720, 160))
    report(f, "Report waiting", "automated-publishing", "'waiting'", task_expr="$json.id",
           message_expr="'Waiting for metadata approval'", pos=(960, 160))  # fmt: skip
    f.chain("Clip ready", "Get task", "Metadata approved?")
    f.link("Metadata approved?", "Publish", 0)
    f.link("Metadata approved?", "Hold for approval", 1)
    f.chain("Publish", "Wait 30 s", "Check upload", "Still uploading?")
    f.link("Still uploading?", "Wait 30 s", 0)
    f.link("Still uploading?", "Report result", 1)
    f.chain("Hold for approval", "Report waiting")
    return f


def scheduled_publishing() -> Flow:
    f = Flow("Snipmint: scheduled publishing", "scheduled-publishing",
             "B: every minute, ask Snipmint what is due (schedules, retries after backoff, missed or newly "
             "approved tasks) and publish each. Snipmint stores every schedule, so an n8n restart loses nothing.")
    f.add("Every minute", "n8n-nodes-base.scheduleTrigger", 1.2,
          {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}})
    f.api("Due tasks", "GET", "/tasks/due?limit=25")
    f.if_("Is a task?", [{"leftValue": "={{ $json.id }}", "rightValue": "",
                          "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}])  # fmt: skip
    f.api("Publish due task", "POST", "/tasks/{{ $json.id }}/publish", body="={{ " + EXEC + " }}",
          idem="={{ $json.id }}:due:{{ $json.updated }}", keep_going=True)  # fmt: skip
    report(f, "Record execution", "scheduled-publishing",
           "($json.outcome === 'started' ? 'success' : ($json.error ? 'error' : 'waiting'))",
           task_expr="$json.id", message_expr="($json.outcome || 'error') + ' - ' + ($json.label || $json.error || '')")
    f.chain("Every minute", "Due tasks", "Is a task?")
    f.link("Is a task?", "Publish due task", 0)
    f.chain("Publish due task", "Record execution")
    return f


def batch_publishing() -> Flow:
    f = Flow("Snipmint: batch publishing", "batch-publishing",
             "C: publish a batch one clip (or `concurrency` clips) at a time with a delay in between; failures "
             "don't stop the rest; report the summary when every clip has an outcome.")
    f.webhook("Batch created", "snipmint/batch")
    f.api("Get batch", "GET", "/batches/{{ $json.body.data.batch_id }}")
    f.add("One item per clip", "n8n-nodes-base.splitOut", 1, {"fieldToSplitOut": "tasks", "options": {}})
    f.add("Loop over clips", "n8n-nodes-base.splitInBatches", 3,
          {"batchSize": "={{ $('Get batch').item.json.concurrency }}", "options": {}})
    f.api("Publish clip", "POST", "/tasks/{{ $json.id }}/publish", body="={{ " + EXEC + " }}",
          idem="={{ $json.id }}:batch:{{ $json.updated }}", keep_going=True, pos=(1200, 160))  # fmt: skip
    f.wait("Delay between uploads", "={{ $('Get batch').item.json.delay_seconds }}", pos=(1440, 160))
    f.wait("Let uploads finish", 60, pos=(1200, -120))
    f.api("Batch summary", "GET", "/batches/{{ $('Get batch').item.json.id }}", pos=(1440, -120))
    f.if_("Finished?", [
        {"leftValue": "={{ !!$json.completed_at || $runIndex >= 120 }}", "rightValue": True,
         "operator": {"type": "boolean", "operation": "true", "singleValue": True}},
    ], pos=(1680, -120))  # fmt: skip
    report(f, "Report batch", "batch-publishing",
           "($json.summary.failed > 0 ? 'error' : ($json.completed_at ? 'success' : 'waiting'))",
           batch_expr="$json.id", message_expr="'Published ' + $json.summary.succeeded + ', failed ' + "
           "$json.summary.failed + ', skipped ' + $json.summary.skipped + ', pending ' + $json.summary.pending",
           pos=(1920, -200))  # fmt: skip
    f.chain("Batch created", "Get batch", "One item per clip", "Loop over clips")
    f.link("Loop over clips", "Let uploads finish", 0)  # output 0 = done
    f.link("Loop over clips", "Publish clip", 1)  # output 1 = next batch of clips
    f.chain("Publish clip", "Delay between uploads", "Loop over clips")
    f.chain("Let uploads finish", "Batch summary", "Finished?")
    f.link("Finished?", "Report batch", 0)
    f.link("Finished?", "Let uploads finish", 1)
    return f


def metadata_approval() -> Flow:
    f = Flow("Snipmint: metadata and approval", "metadata-approval",
             "D: write a clip's metadata with Snipmint's own generator (Gemini or the free builder), then "
             "publish only if it is approved (by you, or automatically if you turned auto-approve on).")
    f.webhook("Metadata requested", "snipmint/metadata")
    f.api("Write metadata", "POST", "/tasks/{{ $json.body.data.task_id }}/metadata",
          body="={{ JSON.stringify({ execution_id: String($execution.id), language: $json.body.data.language }) }}",
          timeout=180000)  # fmt: skip
    f.if_("Approved and due now?", [
        {"leftValue": "={{ $json.approved }}", "rightValue": True,
         "operator": {"type": "boolean", "operation": "true", "singleValue": True}},
        {"leftValue": "={{ !$json.run_at && !$json.batch_id }}", "rightValue": True,
         "operator": {"type": "boolean", "operation": "true", "singleValue": True}},
    ])  # fmt: skip
    f.api("Publish", "POST", "/tasks/{{ $json.id }}/publish", body="={{ " + EXEC + " }}",
          idem="={{ $json.id }}:publish:{{ $json.updated }}", pos=(720, -120))  # fmt: skip
    report(f, "Report", "metadata-approval",
           "($json.approved ? 'success' : 'waiting')", task_expr="$json.id",
           message_expr="($json.approved ? 'Approved' : 'Waiting for approval') + ': ' + ($json.title || '')",
           pos=(960, 80))  # fmt: skip
    f.chain("Metadata requested", "Write metadata", "Approved and due now?")
    f.link("Approved and due now?", "Publish", 0)
    f.link("Approved and due now?", "Report", 1)
    f.link("Publish", "Report")
    return f


FORMAT_JS = r"""const out = [];
for (const item of $input.all()) {
  const ev = item.json.body || {};
  const d = ev.data || {};
  const title = d.title ? `"${d.title}"` : 'A clip';
  let subject, message;
  switch (ev.type) {
    case 'upload.succeeded':
      subject = 'Published on YouTube'; message = `${title} is on YouTube: ${d.video_url || ''}`; break;
    case 'upload.failed':
      subject = 'Upload failed'; message = `${title} could not be uploaded: ${d.error || 'unknown error'}` +
        (d.will_retry ? ' (Snipmint will retry)' : ''); break;
    case 'schedule.triggered':
      subject = 'Scheduled upload started'; message = `${title} (scheduled for ${d.run_at}) is uploading now.`; break;
    case 'batch.completed':
      subject = 'Batch finished'; message = `Batch ${d.batch_id}: ${d.succeeded} published, ${d.failed} failed, ` +
        `${d.skipped} skipped, ${d.pending} pending.`; break;
    case 'metadata.awaiting_approval':
      subject = 'Metadata waiting for approval'; message = `${title}: review and approve its title and description in Snipmint before it can be published.`; break;
    default:
      subject = 'Snipmint'; message = `${ev.type}: ${JSON.stringify(d).slice(0, 300)}`;
  }
  out.push({ json: { type: ev.type, subject, message } });
}
return out;"""

ERROR_JS = r"""const e = $input.first().json;
const name = (e.workflow && e.workflow.name) || 'a workflow';
const err = (e.execution && e.execution.error && e.execution.error.message) || (e.trigger && e.trigger.error && e.trigger.error.message) || 'unknown error';
return [{ json: { subject: `n8n: ${name} failed`, message: `${name} failed: ${err}. Execution ${(e.execution && e.execution.id) || '-'}${e.execution && e.execution.url ? ' ' + e.execution.url : ''}` } }];"""


def notifications() -> Flow:
    f = Flow("Snipmint: publishing notifications", "publishing-notifications",
             "E: turn Snipmint's events (published, failed, schedule started, batch done, waiting for "
             "approval) into a message for Telegram, Discord or e-mail. Enable the channels you use.")
    f.webhook("Snipmint event", "snipmint/notify")
    f.code("Write message", FORMAT_JS)
    f.chain("Snipmint event", "Write message")
    f.notifiers("Write message")
    return f


def error_handling() -> Flow:
    f = Flow("Snipmint: error handling", "error-handling",
             "Set this as the Error Workflow of the other Snipmint workflows: it records the failed execution "
             "in Snipmint (shown on the Publish screen) and can alert you. It never retries uploads itself: "
             "Snipmint decides retries with backoff and an attempt limit.")
    f.add("When a workflow fails", "n8n-nodes-base.errorTrigger", 1, {})
    f.api("Record failure in Snipmint", "POST", "/executions", keep_going=True, body=(
        "={{ JSON.stringify({ execution_id: String(($json.execution && $json.execution.id) || $execution.id), "
        "workflow: ($json.workflow && $json.workflow.name) || 'unknown', status: 'error', "
        "message: String(($json.execution && $json.execution.error && $json.execution.error.message) || 'failed').slice(0, 900) }) }}"))  # fmt: skip
    f.code("Write alert", ERROR_JS)
    f.chain("When a workflow fails", "Record failure in Snipmint")
    f.link("When a workflow fails", "Write alert")
    f.notifiers("Write alert", y=200)
    return f


FLOWS = {
    "automated-publishing.json": automated_publishing,
    "scheduled-publishing.json": scheduled_publishing,
    "batch-publishing.json": batch_publishing,
    "metadata-approval.json": metadata_approval,
    "publishing-notifications.json": notifications,
    "error-handling.json": error_handling,
}


def build() -> dict[str, dict]:
    return {name: fn().dump() for name, fn in FLOWS.items()}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (OUT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {OUT / name}")
