"""Customised single-turn harmful-prompt generation.

Given a model policy, produce prompts that violate it, for red-team and
policy-compliance testing. Local-script first; the Airflow DAG wraps these
modules rather than reimplementing them.
"""
