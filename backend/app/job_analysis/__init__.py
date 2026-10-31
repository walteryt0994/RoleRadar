"""岗位分析: the job analysis pages and their temporary tasks (v0.1 U2-A2).

The pages connect the evidence analysis of U2-A1 (app.experience_analysis)
to the user: choose confirmed experiences, paste one job posting, review
what would go out and to whom, confirm, and read the result with its
sources. Tasks and results stay in this process for a limited time; nothing
is saved, and nothing is sent anywhere but to the configured service.

The experience library (app.library) does not depend on this package; it
only tells it when a session ends.
"""
