class MockOCLCLinkedDataAPI(object):

    def __init__(self):
        self.info_results = []

    def queue_info_for(self, *metadatas):
        self.info_results.append(metadatas)

    def info_for(self, *args, **kwargs):
        return self.info_results.pop(0)


class MockVIAFClient(object):

    def __init__(self):
        self.results = []

    def queue_lookup(self, *result):
        self.results.append(result)

    def lookup_by_viaf(self, *args, **kwargs):
        return self.results.pop(0)
    
    def lookup_by_name(self, *args, **kwargs):
        return self.results.pop(0)
