import sys
import os
import argparse
from pathlib import Path

from azure.identity import AzureCliCredential
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items, change_log_level, append_feature_flag

# Force unbuffered output like `python -u`
sys.stdout.reconfigure(line_buffering=True, write_through=True)
sys.stderr.reconfigure(line_buffering=True, write_through=True)

# Enable debugging if defined in Azure DevOps pipeline
if os.getenv("SYSTEM_DEBUG", "false").lower() == "true":
    change_log_level("DEBUG")

# Assumes your script is one level down from root
root_directory = Path(__file__).resolve().parent.parent

# Enable shortcut publish
append_feature_flag("enable_shortcut_publish")

# Accept parsed arguments
parser = argparse.ArgumentParser(description='Process Azure Pipeline arguments.')
parser.add_argument('--workspace_id', type=str)
parser.add_argument('--environment', type=str)
parser.add_argument('--repository_directory', type=str)
parser.add_argument("--item_type_in_scope", type=str)
args = parser.parse_args()

workspace_id = args.workspace_id
environment = args.environment
repository_directory = args.repository_directory
item_type_in_scope = args.item_type_in_scope.split(",")

token_credential = AzureCliCredential()

# Initialize the FabricWorkspace object with the required parameters
target_workspace = FabricWorkspace(
    workspace_id=workspace_id,
    environment=environment,
    repository_directory=repository_directory,
    item_type_in_scope=item_type_in_scope,
    token_credential=token_credential,
)

# Publish all items defined in item_type_in_scope
publish_all_items(target_workspace)