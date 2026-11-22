"""Local NER annotation governance component.

The public facade is :class:`ner_governance.api.NerGovernance`.  All business
failures are raised as :class:`ner_governance.errors.GovernanceError` carrying a
stable business code and structured details.
"""

from .api import NerGovernance
from .errors import GovernanceError

__all__ = ["NerGovernance", "GovernanceError"]
__version__ = "0.1.0"
