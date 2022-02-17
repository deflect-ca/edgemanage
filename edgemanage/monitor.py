from prometheus_client import CollectorRegistry, Gauge, write_to_textfile


class SingletonMetaclass(type):
    """
    Singleton metaclass to enable Monitor class
    to be used accross different modules
    """
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        # return instance so we can call Monitor().set()
        return cls._instances[cls]


class Monitor(metaclass=SingletonMetaclass):
    """
    Prometheus metrics monitor
    """

    # gauges array with key like: edge1_dev_deflect_network_response_time
    gauges = {}
    suffixs = [
        'response_time',
        'average_time',
        'timeslice',
        'reachable_status',
        'in_rotation',
    ]

    def __init__(self, edges=None, registry=None):
        if registry is None:
            self.registry = CollectorRegistry()

        # for linter
        if edges is not None:
            self.create_gauges(edges)

    def create_gauges(self, edges):
        for edge in edges:
            for suffix in self.suffixs:
                key = f"{self._format(edge)}_{suffix}"
                self.gauges[key] = Gauge(key, '', registry=self.registry)

    def set(self, edge, suffix, value):
        key = f"{self._format(edge)}_{suffix}"
        if key in self.gauges:
            self.gauges[key].set(value)

    def _format(self, edge):
        # Replace . in edge URL so it can be used as a metric name
        return edge.replace('.', '_')

    def write_metrics(self, filepath):
        try:
            write_to_textfile(filepath, self.registry)
        except FileNotFoundError as e:
            print(str(e))
            print("Please set path at 'prometheus_logs' in edgemanage.conf")
