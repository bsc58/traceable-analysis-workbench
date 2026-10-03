"""Safe standalone evidence viewer. Contains no scripts or remote resources."""
import html
import json


def render_record(record: dict) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    def block(title, value, expanded=False):
        return (f'<details {"open" if expanded else ""}><summary>{esc(title)}</summary>'
                f'<pre>{esc(json.dumps(value, indent=2, ensure_ascii=False))}</pre></details>')
    run = record["run"]
    report = record["report"]
    validation = report.get("validation", {}) if report else {}
    body = report.get("report", {}) if report else {}
    sections = [
        '<section id="validation-summary" class="notice">',
        f'<p>校验结果：{esc(validation.get("status", "NOT RUN"))}</p>',
        f'<p>校验范围：{esc(validation.get("scope", "尚未提交结构化事实校验"))}</p>',
        f'<p>result_status：{esc(body.get("result_status", "NOT RUN"))}</p>',
        '<p>数据模式：live (LIVE)</p></section>',
        f'<h1>标题（未校验）：{esc(body.get("title", "分析运行记录"))}</h1>',
        f'<p>{esc(run["state"])} · {esc(run["id"])}</p>',
        '<p>本页回放保存的记录，没有重新查询。live 来源可能变化，此运行不具备冻结重跑能力。</p>']
    if report:
        facts = validation.get("normalized_facts")
        if facts is None:
            # Old artifacts stay intact. Remove their free labels from the facts area.
            facts = [{k: v for k, v in body["facts"][i].items() if k not in {"label", "note"}}
                     for i in validation.get("verified_fact_indices", [])]
        facts = [{k: v for k, v in fact.items() if k != "note"} for fact in facts]
        sections.append('<section id="structured-facts"><h2>结构化事实核对记录</h2>' +
                        block("数值、引用及结构化标签", facts, True) +
                        block("校验问题与必要事实覆盖", {k: v for k, v in validation.items()
                              if k not in {"normalized_facts", "generated_labels"}}) + '</section>')
        text = {"note": [f.get("note", f.get("label", "")) for f in body.get("facts", [])
                         if f.get("note", f.get("label", ""))]}
        text.update({k: body.get(k, []) for k in ("hypotheses", "limitations", "next_checks")})
        sections.append('<section id="unverified-text"><h2>文字区（未校验）</h2>' +
                        block("以下文字均未校验", text, True) + '</section>')
    sections.append(block("输入、版本和预算清单", run["manifest"]))
    sections.append('<h2>查询及补查记录</h2>')
    for index, step in enumerate(record["steps"], 1):
        sections.append(block(f'{index}. {step["decision"]["tool_ref"]} · {step["state"]}', step, True))
    sections.extend([block("完整事件序列", record["events"]), block("绑定的 Skill 发布内容", record["skill"]),
                     block("绑定的指标目录", record["catalog"]), block("绑定的工具契约", record["tools"])])
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; form-action 'none'; base-uri 'none'">
<title>可追溯分析 · 运行记录</title><style>
body{font:16px/1.6 system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 24px;color:#202f40;background:#f6f8fa}
h1{font-size:28px}h2{font-size:20px}details{background:white;border:1px solid #dbe2ea;border-radius:8px;padding:14px;margin:14px 0}
summary{cursor:pointer;font-weight:600}pre{font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.notice{border-left:4px solid #a65d00;background:#fff3df;padding:12px}b{color:#146450}
</style><body>''' + "\n".join(sections) + "</body></html>"
