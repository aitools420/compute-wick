# PRODUCT.md — compute.pangle.online

**What it is.** A live meta-aggregator for GPU rental compute. It polls marketplace
inventory (Vast.ai, RunPod, DataCrunch, Akash), normalizes everything into one offer table,
ranks it, and serves it two ways: a human market browser and an agent path (REST +
MCP). Demand is deliberately routed toward idle/interruptible capacity.

**Who it is for.**
1. Engineers and researchers hunting the cheapest suitable GPU right now.
2. AI agents provisioning their own compute (first-class citizens, not an afterthought).
3. Later: enterprise ESG buyers who need the idle-routing story documented.

**What they do here.** Check live prices, filter to their GPU/region/budget, click
through to the provider, or point their agent at the MCP endpoint. One number to
remember: the idle share, live.

**Why it wins.** Coverage + freshness + the agent path. Zero platform fees at launch
(fee_bps: 0 in every response, verifiable). The environmental story is measured, not
claimed: interruptible listings ARE idle hardware.

**Voice.** Plain, technical-warm, honest. A field journal kept by an engineer.
No invented numbers, no greenwash, no hype verbs.
