"""Job posting sources (scrapers, API clients, etc.).

Importing this package registers all built-in sources (see
`jobsearcher.sources.base.register_source`); anything that looks up a
source by name via `jobsearcher.sources.base.get_source_class` should
import `jobsearcher.sources` first so the registration side effect runs.
"""

from jobsearcher.sources import greenhouse as greenhouse
from jobsearcher.sources import weworkremotely as weworkremotely
