"""hermes-doppler: Doppler Secrets Manager plugin for Hermes Agent.

Re-exports DopplerSource from the hermes_doppler package so Hermes'
directory-based plugin discovery can load it from the repo root.
"""
from hermes_doppler import DopplerSource

__all__ = ["DopplerSource"]
