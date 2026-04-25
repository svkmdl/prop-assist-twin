import logging
from mangum import Mangum
from server import app, LOG_LEVEL

# Ensure the Root Logger (which Lambda uses) matches your desired level
logging.getLogger().setLevel(LOG_LEVEL)

# Create the Lambda handler
handler = Mangum(app)