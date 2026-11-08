"""Agent review of a graded design: the design-review skill's rule check decides, a coding agent
explains.  The same engine runs on a person's own computer, before they submit (the workspace's
`review` job: their own Claude Code or Codex, in its sandbox or in Docker), and on the official
server, which decides again from its own grade (qccd/official/review.py).

    agents.py     the reviewers (Claude Code, Codex), their profiles and sign-in, the sandboxed command
    engine.py     one review: stage, rule check, decision, then the agent
    container.py  Docker isolation: the image, the review's own network, the egress proxy
"""
