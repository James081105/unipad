"""Milestone-gate simulations.

These tests drive the real controller/host/ipa/guest code against the
simulation doubles in ``tests/fakes``.  A green run here means the code
stage behaves as designed -- it is NOT hardware verification (PRD 6:
现场验证状态 stays UNVERIFIED until run on the user's Windows machine).
"""
