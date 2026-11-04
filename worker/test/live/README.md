"""worker/test/live — rerunnable live delivery matrix (see MANIFEST.json).

Run:  python3 worker/test/live/run_matrix.py
      go   test ./worker/test/live/ -run TestLiveMatrixInvariants

The matrix never touches the baseline task's permanent config: every replica
it creates carries a marker in `metadata` and is deleted at the end.
"""
