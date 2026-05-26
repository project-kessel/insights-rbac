# Outputs all bootstrap relationships for a given org ID to a file
# This file can be piped the zed relationship delete in order to delete them
# This is NOT compatible with orgs that have more relationships
# than those that come with bootstrapping and default / admin group memberships.
# That is, once dual write is on or migration has run, this cannot be safely used.
# It may miss relationships or not parse the output relationships correctly.
# Using it of course assumes the org is cleared or re-bootstrapped in RBAC as well.
# To do this, the workspaces and tenant mappings must be removed.
# If there are any bindingmappings, this script cannot be used (it means migration has run or dual write is on)

# This script takes an org_id and assumes a local zed context against the correct environment.

set -e
set -x

org_id=$1
filename="reset_org_spicedb_${org_id}_$(date +%s).txt"

tenant_to_platform=$(zed relationship read rbac/tenant:redhat/$org_id)
root_to_tenant=$(zed relationship read rbac/workspace t_parent rbac/tenant:redhat/$org_id)
root_ws=$(echo $root_to_tenant | egrep -o "^\S+")
default_to_root=$(zed relationship read rbac/workspace t_parent $root_ws)
default_ws=$(echo $default_to_root | egrep -o "^\S+")

default_ws_to_all=$(zed relationship read $default_ws)
role_binding_1=$(echo "$default_ws_to_all" | egrep -o "rbac/role_binding:\S+" | sed -n '1p')
role_binding_2=$(echo "$default_ws_to_all" | egrep -o "rbac/role_binding:\S+" | sed -n '2p')

# Now get each role binding relationships
role_binding_1_to_all=$(zed relationship read $role_binding_1)
role_binding_2_to_all=$(zed relationship read $role_binding_2)

# Now get the group from the subject of the role binding relationships
group_1=$(echo "$role_binding_1_to_all" | egrep -o "rbac/group:[^#]+")
group_2=$(echo "$role_binding_2_to_all" | egrep -o "rbac/group:[^#]+")

# Now get all the relationships for each group
group_1_to_all=$(zed relationship read $group_1)
group_2_to_all=$(zed relationship read $group_2)

# Now write all of these to a file, deduplicated
echo "$tenant_to_platform" > $filename
echo "$root_to_tenant" >> $filename
echo "$default_ws_to_all" >> $filename
echo "$role_binding_1_to_all" >> $filename
echo "$role_binding_2_to_all" >> $filename
echo "$group_1_to_all" >> $filename
echo "$group_2_to_all" >> $filename
