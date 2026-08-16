"""siren — Seattle Fire 911 incident intake for the nemo-nvidia-demo federation.

The class that hot-loads mid-demo from one config file. Live poll of the
Socrata kzjm-xkqj feed when the network is there, a cached snapshot with an
honest as-of label when it is not, and incident-class signals emitted into
throughline either way.
"""

__version__ = "0.2.0"
