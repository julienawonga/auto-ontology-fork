# SPDX-License-Identifier: Apache-2.0

"""Evaluation harness for the text-to-SQL chat and the ontology behind it.

Three pieces, cheapest first:

* ``compare``  -- decide whether two result sets are the same answer.
* ``report``   -- a per-question report and a paired base-vs-candidate diff.
* ``runner``   -- drive a running backend and score the answers.

``python -m dev_tools.evals --help`` lists the commands.
"""
