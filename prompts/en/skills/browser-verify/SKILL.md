---
name: browser-verify
description: Use when HTML/JS/CSS, request headers, form interaction or page rendering change: how to do browser-level verification, and how to write the "Browser verification" section and presentation assertions in the self-test report.
roles: [dev]
activation: always
paths: ["static/**", "templates/**", "**/*.html", "**/*.css", "**/*.js"]
keywords: [page, style, browser, frontend]
required: false
owner: loop-engine
status: active
---

Browser verification (the self-test report must have a separate "Browser verification" section):
Browser-side changes (diffs touching embedded HTML/JS/CSS, fetch/HTTP request headers, form interaction, page rendering logic or anything else affecting browser behavior) must get a separate "Browser verification" section: unit tests, curl end-to-end and JS syntax checks are not browser-level verification, and you must not claim "verified in browser" —
First confirm whether this session's tool list really has browser tools (mcp__playwright__browser_*, in the allow list). If it does, you must actually open a browser and walk the main path: start a test instance (http://127.0.0.1:<test port>) → open the page with browser_navigate → key interactions (browser_click/browser_type/browser_fill_form, etc., as the scenario needs) → confirm with browser_console_messages that the console has no errors → write the test instance address/steps/results into that section; if it does not (the prefix is allowed but this machine did not inject the MCP server), use the project's own browser driver (project Playwright/headless Chrome, CDP scripts, etc.) to do the same main-path verification, and state the driver, version and command in the section. `file://` reference pages cannot be opened with browser tools (approval only allows http(s)); render reference pages with a script or compare against their source.
When the change touches the presentation layer (colors/icons/label text/status styles and other "display semantics of the same element", or the acceptance criteria contain such requirements), the section must also contain at least one **decidable presentation assertion**: either actually look at the result with browser_take_screenshot and write "what was seen, whether it matches expectations", or assert on the rendered result using the project conventions' render assertion method and cite the test case name; "renders fine" or "displays correctly" alone is not an assertion. When you change the semantics of a label/badge (source, priority, value), you must also check whether other display attributes on the same element (color/icon/border) are still bound to the old field, and state the conclusion.
When tools are truly unavailable (no local browser environment/tool failure), or the project conventions provide no test instance, write "Not done, needs manual acceptance" truthfully in the section and declare todos in result.json (e.g. "todos": ["Open page xx in a browser and verify xx"]); the two must agree.
