import os
import site
import sys
d = os.path.split(__file__)[0]
site.addsitedir(os.path.join(d, ".."))
from integration.overdrive import (
    OverdriveCirculationMonitor,
)
from model import production_session

if __name__ == '__main__':
<<<<<<< HEAD
    path = sys.argv[1]      
=======
>>>>>>> 97b46d6444f434d49ca18dd7f476b0782adfdeb4
    session = production_session()
    OverdriveCirculationMonitor(session).run(session)
