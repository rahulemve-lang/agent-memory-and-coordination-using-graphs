#!/bin/sh
# One-shot seeding for n8n: creates the credentials and imports the four workflows.
# Runs in the n8n-init service before n8n starts. Skips itself after the first run so
# it never deactivates workflows you have switched on; set N8N_RESEED=1 to force it.
set -eu

MARK=/home/node/.n8n/.graphmem-seeded
if [ -f "$MARK" ] && [ "${N8N_RESEED:-0}" != "1" ]; then
  echo "n8n already seeded (set N8N_RESEED=1 to re-import) - skipping"
  exit 0
fi

echo "Writing credentials from environment..."
node -e '
const need = (k) => process.env[k] || "";
const creds = [
  { id: "graphmem-key", name: "graphmem API key", type: "httpHeaderAuth",
    data: { name: "X-API-Key", value: need("GRAPHMEM_API_KEY") } },
  { id: "gemini-key", name: "Gemini API", type: "googlePalmApi",
    data: { host: "https://generativelanguage.googleapis.com", apiKey: need("GEMINI_API_KEY") } },
  { id: "groq-key", name: "Groq API", type: "groqApi",
    data: { apiKey: need("GROQ_API_KEY") } },
];
require("fs").writeFileSync("/tmp/credentials.json", JSON.stringify(creds));
'

n8n import:credentials --input=/tmp/credentials.json
rm -f /tmp/credentials.json

echo "Importing workflows..."
n8n import:workflow --separate --input=/workflows

touch "$MARK"
echo "Done. Open http://localhost:5678, test each workflow, then activate 2 and 3."
