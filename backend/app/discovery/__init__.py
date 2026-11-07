"""找岗位: searching for postings for a user, step by step (v0.1 U3).

U3-A1 laid the ground, all of it offline: the contracts of the tools
(contracts), the register of sources and what each may be used for
(sources), frozen synthetic postings to test with (synthetic), the fixed
rules the tools apply (rules) and the tools themselves (tools). U3-A2 adds
the loop deciding each next step (agent), the fixed flow it is compared with
(baseline), searches as tasks of one user and one session (tasks), and
running one as a single parent task behind its service (runner). There is no
page yet (A3) and no real source: every source a tool can call is synthetic,
nothing goes over the network, and no real model has decided anything.
"""
