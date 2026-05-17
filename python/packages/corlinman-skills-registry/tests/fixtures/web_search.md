---
name: web_search
description: Search the web via Brave Search API
metadata:
  openclaw:
    emoji: "🔍"
    requires:
      bins: []
      anyBins: []
      config: ["providers.brave.api_key"]
      env: []
    install: "Get an API key at https://brave.com/search/api/"
allowed-tools:
  - web.search
  - web.fetch
---
# Web Search

Use the `web.search` tool to find information on the internet.

Return concise summaries of the top results.
