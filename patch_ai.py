import os

file_path = "apps/ai_agent/services.py"
with open(file_path, "r") as f:
    content = f.read()

content = content.replace("def _call_claude(self, product, variation_index: int = 0) -> dict:", "def _call_claude(self, product, plan_slug: str, variation_index: int = 0) -> dict:")
content = content.replace("model='claude-sonnet-4-20250514',", "model='claude-3-5-sonnet-latest' if plan_slug == 'pro' else 'claude-3-5-haiku-latest',")

content = content.replace("def _call_openai(self, product, variation_index: int = 0) -> dict:", "def _call_openai(self, product, plan_slug: str, variation_index: int = 0) -> dict:")
content = content.replace("model='gpt-4o',", "model='gpt-4o' if plan_slug == 'pro' else 'gpt-4o-mini',")

with open(file_path, "w") as f:
    f.write(content)

print("Patched successfully")
