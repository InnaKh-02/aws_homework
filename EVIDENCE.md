# Evidence for AgentCore E2E Deployment (Level 3)

Цей документ збирає підтвердження реального виконання тестів у прод deploy середовищі.
Заповніть розділи нижче власними результатами, логами та скрінами.

## 1. Деплой і старт системи

- Дата виконання: 2026/05/28
- Оточення (локально / cloud):
- Посилання на runtime arn: "arn:aws:bedrock-agentcore:us-east-1:501578625560:runtime/acwslite_runtime_agent-hua58VGuZU"
- Посилання на gateway arn: "arn:aws:bedrock-agentcore:us-east-1:501578625560:gateway/acwslite-gateway-4kptx9klrv"
- Наявність Google Docs target: `GOOGLE_DOC_ID` = 1mSlxulNixYH0uUOn9fVhbys33h-Vg0hr8mmCAM_mubk
- Чи пройдено початкову конфігурацію з `workshop_google_docs_rag_e2e.ipynb`: так

### 1.1 Підтвердження deploy
- Надано output, що підтверджує успішний deploy:

```bash
┌──────────────────────────── Deployment Success ─────────────────────────────┐
│ Agent Details:                                                              │
│ Agent Name: acwslite_runtime_agent                                          │
│ Agent ARN:                                                                  │
│ arn:aws:bedrock-agentcore:us-east-1:501578625560:runtime/acwslite_runtime_a │
│ gent-hua58VGuZU                                                             │
│ Deployment Type: Direct Code Deploy                                         │
│                                                                             │
│ 📦 Code package deployed to Bedrock AgentCore                               │
│                                                                             │
│ Next Steps:                                                                 │
│    agentcore status                                                         │
│    agentcore invoke '{"prompt": "Hello"}'                                   │
│                                                                             │
│ 📋 CloudWatch Logs:                                                         │
│    /aws/bedrock-agentcore/runtimes/acwslite_runtime_agent-hua58VGuZU-DEFAUL │
│ T --log-stream-name-prefix "2026/05/28/[runtime-logs"                       │
│    /aws/bedrock-agentcore/runtimes/acwslite_runtime_agent-hua58VGuZU-DEFAUL │
│ T --log-stream-names "otel-rt-logs"                                         │
│                                                                             │
│ 🔍 GenAI Observability Dashboard:                                           │
│    https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#gen-ai-o │
│ bservability/agent-core                                                     │
│                                                                             │
│ ⏱️  Note: Observability data may take up to 10 minutes to appear after      │
│ first launch                                                                │
│                                                                             │
│ 💡 Tail logs with:                                                          │
│    aws logs tail                                                            │
│ /aws/bedrock-agentcore/runtimes/acwslite_runtime_agent-hua58VGuZU-DEFAULT   │
│ --log-stream-name-prefix "2026/05/28/[runtime-logs" --follow                │
│    aws logs tail                                                            │
│ /aws/bedrock-agentcore/runtimes/acwslite_runtime_agent-hua58VGuZU-DEFAULT   │
│ --log-stream-name-prefix "2026/05/28/[runtime-logs" --since 1h              │
└─────────────────────────────────────────────────────────────────────────────┘
⏳ Runtime acwslite_runtime_agent-hua58VGuZU status: READY
✅ Runtime ready: {
  "runtime_id": "acwslite_runtime_agent-hua58VGuZU",
  "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:501578625560:runtime/acwslite_runtime_agent-hua58VGuZU",
  "runtime_status": "READY"
}
```

## 2. Перший invoke (consent challenge або consent flow)

Опишіть перший запит, який підтверджує, що consent flow ідентифікований і ініційований.

- Дата першого invoke: 2026-04-15
- authorization_url: https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize?request_uri=urn%3Aietf%3Aparams%3Aoauth%3Arequest_uri%3AMjgxYjg4ZDQtZjgzZC00MjdjLWFhODktNjBhOGMyZTdlMDk2
- oauth_session_uri: urn:ietf:params:oauth:request_uri:MjgxYjg4ZDQtZjgzZC00MjdjLWFhODktNjBhOGMyZTdlMDk2

### Результат
- HTTP статус: 200
- Тіло відповіді (скопіюйте ключову частину):
  - Tool trace:
    - step=1 event=tool_call tool=get_google_doc
    - step=2 event=tool_result tool=get_google_doc
    expected_app_version: 2026-04-15-runtime-minimal-deps-v10

✅ Local callback server ready:
{
  "url": "http://localhost:8081/oauth2/callback",
  "host": "localhost",
  "port": 8081,
  "path": "/oauth2/callback",
  "status": "running"
}

## 3. Другий invoke (успішна відповідь із sources)

Опишіть другий запит, який має повернути коректний результат із полем `sources` та слідом tool call.

- Дата другого invoke:
- consent_required: False
- response:
 - The cheetah is the fastest mammal in the world, capable of running as fast as 70 miles (110 kilometers) per hour.Cheetahs can accelerate from 0 to 45 miles (72 kilometers) per hour in two seconds and maintain their top speed for up to 300 yards (274 meters).
- Sources: https://docs.google.com/document/d/1mSlxulNixYH0uUOn9fVhbys33h-Vg0hr8mmCAM_mubk/edit

### Результат
- HTTP статус: 200
- Tool trace:
  - step=1 event=tool_call tool=get_google_doc
  - step=2 event=tool_result tool=get_google_doc
  expected_app_version: 2026-04-15-runtime-minimal-deps-v10

## 3. Cleanup

Опишіть очищення ресурсів після тестів.

- Команди cleanup:
  - `...`
- Ресурси, які видалено:
  - Runtime app / deployment
  - Gateway configuration
  - OAuth consent state / локальні токени
  - Тимчасові артефакти (`tmp/`, `.bedrock_agentcore*`, `vertex-credentials.json` if temporary)
- Статус cleanup (успішно / з помилками):
- Додаткові зауваження:
  - `...`
