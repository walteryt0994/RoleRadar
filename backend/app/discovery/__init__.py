"""找岗位: searching for postings for a user, step by step (v0.1 U3).

U3-A1 lays only the ground a search agent will stand on, all of it offline:
the contracts of its tools (contracts), the register of sources and what
each may be used for (sources), frozen synthetic postings to test with
(synthetic), the fixed rules the tools apply (rules) and the tools
themselves (tools). There is no loop deciding the next step yet (A2), no
page (A3) and no real source: every source a tool can call is synthetic,
and nothing goes over the network.
"""
