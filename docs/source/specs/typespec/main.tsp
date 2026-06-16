import "@typespec/http";
import "@typespec/rest";
import "@typespec/openapi3";
import "@typespec/versioning";

using TypeSpec.Http;
using TypeSpec.Rest;
using TypeSpec.Versioning;

@service(#{title:"Role Based Access Control for Kessel Project"})

@server("https://localhost:{port}/{basePath}", "Development Server", { basePath: string="api/rbac/v2", port: string="8000" })
@server("https://console.stage.redhat.com/{basePath}", "Stage Server", { basePath: string="api/rbac/v2" })
@server("https://console.redhat.com/{basePath}", "Production Server", { basePath: string="api/rbac/v2" })
@versioned(Versions)

namespace KesselRBAC;

enum Versions{
    v2,
}

@doc("Exclude bindings by source type: 'none' (default) shows all, 'indirect' excludes inherited, 'direct' excludes direct")
enum ExcludeSources {
    direct,
    indirect,
    none,
}

model Timestamps {
    @doc("2019-01-21T17:32:28Z")
    created: offsetDateTime;
    @doc("2019-01-21T17:32:28Z")
    modified: offsetDateTime;
}

@format("uuid")
scalar UUID extends string;

@example(#{
    application: "rbac",
    resource_type: "workspace",
    operation: "read"
})
model Permission {
    application: string;
    resource_type: string;
    operation: string;
}

@example(#{
    id: "550e8400-e29b-41d4-a716-446655440002",
    name: "Workspace Admin",
    description: "Full administrative access to workspace",
    permissions: #[
        #{
            application: "rbac",
            resource_type: "workspace",
            operation: "read"
        },
        #{
            application: "rbac",
            resource_type: "workspace",
            operation: "write"
        }
    ],
    permissions_count: 2,
    last_modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
})
model Role {
    @doc("UUID that uniquely identifies the role")
    id?: UUID;
    @doc("A human readable name for the role. Must be unique within a tenant.")
    name?: string;
    @doc("A description of the role to help clarify its purpose. Does not need to be unique.")
    description?: string;
    @doc("List of permissions assigned to this role")
    permissions?: KesselRBAC.Permission[];
    @doc("Number of permissions assigned to this role")
    permissions_count?: int32;
    @doc("Timestamp of when the role was last modified")
    last_modified?: offsetDateTime;
    @doc("ID of the organization that owns this role. Will not be populated if roles belong to the Public Tenant.")
    org_id?: string;
}

/**
 * Field mask used by the `fields` query parameter to control which fields
 * are included in the response.
 *
 * **Syntax:**
 * - Use comma to separate multiple fields: `id,name,description`
 * - Use parentheses for nested objects: `subject(id,name),resource(type)`
 *
 * **Examples:**
 * - `id,name`
 * - `subject(group.name),role(name)`
 */
scalar FieldMask extends string;

/**
 * Limit for pagination. Controls the maximum number of items returned.
 * Use -1 to return all objects.
 */
scalar Limit extends int64;

/**
 * Cursor for cursor-based pagination.
 * An opaque string that points to the next/previous page of results.
 */
scalar Cursor extends string;

/**
 * Offset for offset-based pagination.
 * The number of items to skip before starting to return results.
 */
scalar Offset extends int64;

/**
 * Sort order specification for list operations.
 * Use comma to separate multiple fields, prefix with '-' for descending order.
 *
 * **Examples:**
 * - `name` - Sort by name ascending
 * - `-last_modified` - Sort by last_modified descending
 * - `name,-created` - Sort by name ascending, then created descending
 */
scalar OrderBy extends string;

@doc("Resource type for role binding operations. 'workspace' for workspace-scoped bindings (resource_id is workspace UUID). 'tenant' for tenant-scoped bindings (resource_id is tenant resource ID in format {domain}/{org_id}, e.g. redhat/12345).")
enum ResourceType {
    workspace,
    tenant,
}

// Default pagination values
const DefaultLimit: Limit = 10;
const DefaultOffset: Offset = 0;

model ListResponse<Item, StatusCode extends int32 = 200> {
    @statusCode _: StatusCode;
    meta: CursorPaginationMeta;
    links: CursorPaginationLinks;
    data: Item[];
}

model ItemResponse<Item, StatusCode extends int32> {
    @statusCode _: StatusCode;
    ...Item;
}

model CursorPaginationMeta {
    @doc("Limit of returned objects. Use -1 to return all objects.")
    @example(10)
    limit: Limit = DefaultLimit;
}

model CursorPaginationLinks {
    @doc("The next link in pagination")
    @example("/api/rbac/v2/(resource)/?limit=10&cursor=eyJpZCI6IjEyMyJ9")
    @format("uri")
    next: string | null;

    @doc("The previous link in pagination")
    @example("/api/rbac/v2/(resource)/?limit=10&cursor=eyJpZCI6IjEwMCJ9")
    @format("uri")
    previous: string | null;
}

model OffsetPaginationMeta {
    @doc("Total count of objects")
    @example(10)
    count: int64;

    @doc("Limit of returned objects. Use -1 to return all objects.")
    @example(10)
    limit: Limit = DefaultLimit;

    @doc("Offset of returned objects")
    @example(0)
    offset: Offset = DefaultOffset;
}

model OffsetPaginationLinks {
    @doc("The first link in pagination")
    @example("/api/rbac/v2/workspaces/?limit=10&offset=0")
    @format("uri")
    first: string | null;

    @doc("The next link in pagination")
    @example("/api/rbac/v2/workspaces/?limit=10&offset=10")
    @format("uri")
    next: string | null;

    @doc("The previous link in pagination")
    @example("/api/rbac/v2/workspaces/?limit=10&offset=0")
    @format("uri")
    previous: string | null;

    @doc("The last link in pagination")
    @example("/api/rbac/v2/workspaces/?limit=10&offset=90")
    @format("uri")
    last: string | null;
}

namespace Problems {
    enum ProblemType {
        InsufficientPermission:"http://project-kessel.org/problems/insufficient-permission",
    }

    @error
    model ProblemDetails<Status extends valueof int16> {
        @header("content-type") contentType: "application/problem+json";
        type?: ProblemType;

        @statusCode _: typeof Status;
        status?: typeof Status;

        title?: string;
        detail?: string;

        @format("uri")
        instance?: string;
    }

    @error
    model Problem403 extends ProblemDetails<Status = 403>{
        type: ProblemType.InsufficientPermission,
        title: "You do not have permission to perform this action.";
    }

    alias Problem401 = ProblemDetails<Status = 401>;

    @error
    model Problem404 extends ProblemDetails<Status = 404>{
        title: "Resource was not found",
        @example("Not found")
        detail: string
    }

    alias Problem500 = ProblemDetails<Status = 500>;

    @error
    model Problem400 extends ProblemDetails<Status = 400>{
        title: "The request payload contains invalid syntax.",
        @example("JSON parse error - Expecting value: line 1 column 1 (char 0)")
        detail: string
    }

    alias Problem400AlreadyExists = ProblemDetails<Status = 400>;

    alias CommonProblems = Problem400 | Problem401 | Problem403 | Problem500;

    namespace Workspace {
        model Problem400WorkspaceNotEmpty extends Problems.ProblemDetails<Status = 400>{
            title: "Unable to delete due to workspace dependencies",
        }
    }
}

@route("/workspaces/")
@tag("Workspaces")
@doc("Operations about workspaces")
namespace Workspaces {
    @example(#{ name: "My First Workspace", description: "This is a basic workspace." })
    model BasicWorkspace {
        @doc("Workspace A")
        name: string = "Workspace A";
        @doc("Description of Workspace A")
        description?: string = "Description of Workspace A";
    }

    enum WorkspaceTypes {
        "root",
        "default",
        "standard",
        "ungrouped-hosts"
    }

    enum WorkspaceTypesQueryParam {
        "all",
        ...WorkspaceTypes
    }
    @example(#{
        id: "e4277742-b91c-43f1-a185-b827e8574345",
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        type: WorkspaceTypes.standard,
        name: "My first workspace",
        description: "This is a basic workspace.",
        created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
    })
    model Workspace {
        @key id: UUID;
        parent_id?: UUID;
        type: WorkspaceTypes;
        ...BasicWorkspace;
        ...Timestamps;
    }

    model WorkspaceAncestor {
        @key id: UUID;
        parent_id?: UUID;
        name: string;
    }

    @example(#{
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        name: "Alpha Workspace",
        description: "Create a standard workspace."
    })
    model CreateWorkspaceRequest extends BasicWorkspace{
        //if parent_id is not stated within the request for standard workspaces, it is automatically populated from default workspace.
        @doc("Parent ID of Workspace A")
        parent_id?: UUID;
    }
    @example(#{
        _: 201,
        id: "e4277742-b91c-43f1-a185-b827e8574345",
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        type: Workspaces.WorkspaceTypes.standard,
        name: "Alpha Workspace",
        description: "Create a standard workspace.",
        created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
    })
    model CreateWorkspaceResponse extends Workspace{
        @statusCode _: 201;
    }
    @example(#{
        _: 200,
        id: "e4277742-b91c-43f1-a185-b827e8574345",
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        type: Workspaces.WorkspaceTypes.standard,
        name: "Alpha Workspace",
        description: "Create a standard workspace.",
        created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
    })
    model ReadWorkspaceResponse extends Workspace {
        @statusCode _: 200;
    }
    @example(#{
        ancestry: #[
            #{
                id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
                parent_id: "a1b2c3d4-e5f6-47a8-b9c0-d1e2f3a4b5c6",
                name: "Default Workspace"
            }
        ],
        _: 200,
        id: "e4277742-b91c-43f1-a185-b827e8574345",
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        type: Workspaces.WorkspaceTypes.standard,
        name: "Alpha Workspace",
        description: "Create a standard workspace.",
        created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
    })
    model ReadWorkspaceWithAncestryResponse extends ReadWorkspaceResponse {
        ancestry: WorkspaceAncestor[];
    }

    @example(#{
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        name: "Alpha Workspace",
        description: "Changing description of alpha workspace to something new."
    })
    model UpdateWorkspaceRequest {
        @doc("Workspace A")
        name: string = "Workspace A";
        @doc("Description of Workspace A")
        description: string = "Description of Workspace A";
        @doc("Must match current parent_id value. Not updatable directly on the workspace.")
        parent_id: UUID;
    }
    @example(#{
        name: "Alpha Workspace - closed",
        description: "Create a standard workspace."
    })
    model PatchWorkspaceRequest {
        @doc("Workspace A")
        name?: string = "Workspace A";
        @doc("Description of Workspace A")
        description?: string = "Description of Workspace A";
    }
    @example(#{
        _: 200,
        id: "e4277742-b91c-43f1-a185-b827e8574345",
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        type: Workspaces.WorkspaceTypes.standard,
        name: "Alpha Workspace",
        description: "Changing description of alpha workspace to something new.",
        created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
    })
    model UpdateWorkspaceResponse extends Workspace{
        @statusCode _: 200;
    }

    @example(#{
        _: 200,
        id: "e4277742-b91c-43f1-a185-b827e8574345",
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        type: Workspaces.WorkspaceTypes.standard,
        name: "Alpha Workspace - closed",
        description: "Create a standard workspace.",
        created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
    })
    model PatchWorkspaceResponse extends Workspace{
        @statusCode _: 200;
    }

    @example(#{ statusCode: 204 })
    model DeleteWorkspaceResponse{
        @statusCode
        statusCode: 204,
    }

    @example(#{ parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11" })
    model MoveWorkspaceRequest {
      @doc("The UUID of the new parent workspace.")
      parent_id: UUID;
    }
    @example(#{
        _: 200,
        id: "e4277742-b91c-43f1-a185-b827e8574345",
        parent_id: "f0e1d2c3-b4a5-4678-9a1b-c2d3e4f5a6b7"
    })
    model MoveWorkspaceResponse{
        @statusCode _: 200;
        @doc("Workspace ID of moved workspace")
        @key id: UUID;
        @doc("New parent ID of moved workspace")
        parent_id: UUID;
    }

    model Pagination {
        @doc("Pagination metadata")
        meta: OffsetPaginationMeta;

        @doc("Pagination links")
        links: OffsetPaginationLinks;
    }


    @example(#{
        meta: #{
            count: 100,
            limit: 10,
            offset: 0
        },
        links: #{
            first: "/api/rbac/v2/workspaces/?limit=10&offset=0",
            next: "/api/rbac/v2/workspaces/?limit=10&offset=10",
            previous: null,
            last: "/api/rbac/v2/workspaces/?limit=10&offset=90"
        },
        data: #[
            #{
                id: "e4277742-b91c-43f1-a185-b827e8574345",
                parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
                type: WorkspaceTypes.standard,
                name: "My first workspace",
                description: "This is a basic workspace.",
                created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
                modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
            },
        ]
    })
    model WorkspaceListResponse {
        ...Pagination;

        @doc("List of workspaces")
        data: Workspace[];
    }

    @doc("List workspaces in a tenant")
    @summary("List workspaces in a tenant")

    @opExample(
      #{
        parameters: #{
          limit: 10,
          offset: 0
        },
        returnType: #{
          meta: #{
            count: 100,
            limit: 10,
            offset: 0
          },
          links: #{
            first: "/api/rbac/v2/workspaces/?limit=10&offset=0",
            next: "/api/rbac/v2/workspaces/?limit=10&offset=10",
            previous: null,
            last: "/api/rbac/v2/workspaces/?limit=10&offset=90"
          },
          data: #[
            #{
              id: "e4277742-b91c-43f1-a185-b827e8574345",
              parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
              type: WorkspaceTypes.standard,
              name: "My first workspace",
              description: "This is a basic workspace.",
              created: offsetDateTime.fromISO("2024-05-06T12:20-12-0700"),
              modified: offsetDateTime.fromISO("2024-05-06T12:20-12-0700")
            }
          ]
        }
      }
    )
    @get op list(
        @query limit?: Limit = DefaultLimit;

        @doc("Offset for offset-based pagination.")
        @query offset?: Offset = DefaultOffset;

        @doc("Filter by workspace type. Supports comma-separated values (e.g. type=standard,ungrouped-hosts). Defaults to all when not supplied. Case-insensitive.")
        @query type?: string = "all";
        @doc("Filter by workspace name. Case-insensitive substring match by default; use * for glob patterns (e.g. foo*).")
        @query name?: string;
        @doc("Filter workspaces by parent workspace UUID. Returns only direct children of the specified workspace. Useful for lazy-loading a nested tree structure.")
        @query parent_id?: UUID;
        @doc("Filter workspaces by one or more comma-separated UUIDs. Defaults to type=standard unless type is explicitly specified.")
        @query(#{explode: false}) ids?: UUID[];

        @doc("Sort by specified field(s), prefix with '-' for descending order. Allowed fields: name, created, modified, type.")
        @example("-created")
        @query order_by?: string = "name";
    ): WorkspaceListResponse | Problems.CommonProblems;

    @doc("Create workspace in tenant")
    @summary("Create workspace in tenant")
    @opExample(#{
        parameters: #{
                body: #{
                    name: "Alpha Workspace",
                    description: "Create a standard workspace.",
                }
        },
        returnType: #{
            _: 201,
            id: "e4277742-b91c-43f1-a185-b827e8574345",
            parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
            type: Workspaces.WorkspaceTypes.standard,
            name: "Alpha Workspace",
            description: "Create a standard workspace.",
            created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
            modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
            }
      }
    )
    @post op create(@body body: CreateWorkspaceRequest): CreateWorkspaceResponse | Problems.CommonProblems;

    @doc("Get a workspace in tenant")
    @summary("Get a workspace in tenant")
    @route("{id}/")
    // example for when we want to include ancestry
    @opExample(#{
        parameters: #{
            id: "e4277742-b91c-43f1-a185-b827e8574345",
            include_ancestry: true
        },
        returnType: #{
            ancestry: #[
                #{
                    id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
                    parent_id: "a1b2c3d4-e5f6-47a8-b9c0-d1e2f3a4b5c6",
                    name: "Default Workspace"
                }
            ],
            _: 200,
            id: "e4277742-b91c-43f1-a185-b827e8574345",
            parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
            type: Workspaces.WorkspaceTypes.standard,
            name: "Alpha Workspace",
            description: "Create a standard workspace.",
            created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
            modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
        }
    })
    //example for when we want to not include ancestry
    @opExample(#{
        parameters: #{
            id: "e4277742-b91c-43f1-a185-b827e8574345",
            include_ancestry: true
        },
        returnType: #{
            _: 200,
            id: "e4277742-b91c-43f1-a185-b827e8574345",
            parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
            type: Workspaces.WorkspaceTypes.standard,
            name: "Alpha Workspace",
            description: "Create a standard workspace.",
            created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
            modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
        }
    })
    @get op read(
        @doc("Unique identification")
        @path id: UUID;

        @doc("When true, the response will include the ancestry of the workspace.")
        @query include_ancestry?: boolean;
    ): ReadWorkspaceResponse | ReadWorkspaceWithAncestryResponse | Problems.CommonProblems | Problems.Problem404;


    @doc("Edit a workspace")
    @summary("Edit the workspace name or description")
    @route("{id}/")
    @opExample(#{
        parameters: #{
            id: "e4277742-b91c-43f1-a185-b827e8574345",
            body: #{
                parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
                name: "Alpha Workspace",
                description: "Changing description of alpha workspace to something new."
            }
        },
        returnType: #{
       _: 200,
        id: "e4277742-b91c-43f1-a185-b827e8574345",
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        type: Workspaces.WorkspaceTypes.standard,
        name: "Alpha Workspace",
        description: "Changing description of alpha workspace to something new.",
        created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
            }
      }
    )
    @put op update(
        @doc("Unique identification")
        @path id: UUID;
        @body body: UpdateWorkspaceRequest
    ): UpdateWorkspaceResponse | Problems.CommonProblems | Problems.Problem400AlreadyExists;

    @opExample(#{
        parameters: #{
            id: "e4277742-b91c-43f1-a185-b827e8574345",
            body: #{
                name: "Alpha Workspace - closed",
                description: "Create a standard workspace."
            }
        },
        returnType: #{
        _: 200,
        id: "e4277742-b91c-43f1-a185-b827e8574345",
        parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11",
        type: Workspaces.WorkspaceTypes.standard,
        name: "Alpha Workspace - closed",
        description: "Create a standard workspace.",
        created: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
        }
      }
    )
    @route("{id}/")
    @patch(#{implicitOptionality: true})
    op patch(
        @doc("Unique identification")
        @path id: UUID;
        @body body: PatchWorkspaceRequest
    ): PatchWorkspaceResponse | Problems.CommonProblems | Problems.Problem400AlreadyExists;

    @doc("Delete a workspace")
    @summary("Delete the workspace")
    @route("{id}/")
    @opExample(#{
        parameters: #{
            id: "e4277742-b91c-43f1-a185-b827e8574345"
        },
        returnType: #{
            statusCode: 204
        }
      }
    )
    @delete op delete(
        @doc("Unique identification")
        @path id: UUID;
    ): DeleteWorkspaceResponse | Problems.CommonProblems | Problems.Workspace.Problem400WorkspaceNotEmpty;

    @doc("Move a workspace to a new parent.")
    @summary("Move a workspace to a new parent.")
    @route("{id}/move/")
    @opExample(#{
        parameters: #{
            id: "e4277742-b91c-43f1-a185-b827e8574345",
            body: #{
                parent_id: "c1f729e2-3e2b-4f9e-b247-a4b568393e11"
            }
        },
        returnType: #{
            _: 200,
            id: "e4277742-b91c-43f1-a185-b827e8574345",
            parent_id: "f0e1d2c3-b4a5-4678-9a1b-c2d3e4f5a6b7"
            }
      }
    )
    @post op move(
        @doc("Unique identification of the workspace to move")
        @path id: UUID;
        @body body: MoveWorkspaceRequest
    ): MoveWorkspaceResponse
      | Problems.CommonProblems
      | Problems.Problem404;
}

@route("/role-bindings/")
@tag("Role Bindings")
@doc("Operations about role bindings")
namespace RoleBindings {
    const DefaultRoleBindingBySubjectFieldMask: FieldMask = "resource(id),subject(id,type),roles(id)";

    @doc("Subject kind for create/batch, by-subject, and list filters on the binding's subject (group UUID or user/principal UUID). Does not include principal as a separate type string; for external user ID use GET /role-bindings/ with granted_subject_type=principal and granted_subject.principal.user_id.")
    enum BindingSubjectType {
        user,
        group,
    }

    @doc("Effective-grant filter for GET /role-bindings/ only. With type principal, use query granted_subject.principal.user_id. Cannot be combined with subject_type/subject_id on the same request. Not used on /role-bindings/by-subject/.")
    enum GrantedSubjectFilterType {
        user,
        group,
        principal,
    }

    @discriminator("type")
    model BaseSubject {
        @doc("Subject identifier")
        id?: UUID;
        @doc("Type of subject")
        type: string;
    }

    model UserDetails {
        @doc("Name of the user")
        username?: string;
    }

    model UserSubject extends BaseSubject {
        @doc("Type of subject")
        type: "user";
        @doc("User details")
        user?: UserDetails;
    }

    model GroupDetails {
        @doc("Name of the group")
        name?: string;
        @doc("Description of the group")
        description?: string;
        @doc("Number of users in the group")
        user_count?: int64;
    }

    model GroupSubject extends BaseSubject {
        @doc("Type of subject")
        type: "group";
        @doc("Group details")
        group?: GroupDetails;
    }

    alias Subject = UserSubject | GroupSubject;



    model Resource {
        @doc("Resource identifier")
        id?: string;
        @doc("Resource name")
        name?: string;
        @doc("Resource type")
        @example("workspace")
        type?: string;
    }

    @doc("Request body for creating role bindings")
    @example(#{
        resource: #{ id: "550e8400-e29b-41d4-a716-446655440001", type: "workspace" },
        subject: #{ id: "3fa85f64-5717-4562-b3fc-2c963f66afa6", type: BindingSubjectType.group },
        role: #{ id: "550e8400-e29b-41d4-a716-446655440002" }
    })
    @example(#{
        resource: #{ id: "redhat/12345", type: "tenant" },
        subject: #{ id: "3fa85f64-5717-4562-b3fc-2c963f66afa6", type: BindingSubjectType.group },
        role: #{ id: "550e8400-e29b-41d4-a716-446655440002" }
    })
    model CreateRoleBindingsRequest {
        @doc("Resource to bind the role to")
        resource: {
            @doc("Identifier of the resource")
            id: string;
            @doc("Type of resource")
            @example("workspace")
            type: string;
        };
        @doc("Subject to grant access to")
        subject: {
            @doc("UUID that uniquely identifies the subject")
            id: UUID;
            @doc("Type of subject")
            type: BindingSubjectType;
        };
        @doc("Role to assign")
        role: {
            @doc("UUID that uniquely identifies the role")
            id: UUID;
        };
    }

    @doc("Batch request to create multiple role bindings")
    @example(#{
        requests: #[
            #{
                resource: #{ id: "550e8400-e29b-41d4-a716-446655440001", type: "workspace" },
                subject: #{ id: "3fa85f64-5717-4562-b3fc-2c963f66afa6", type: BindingSubjectType.group },
                role: #{ id: "550e8400-e29b-41d4-a716-446655440002" }
            },
            #{
                resource: #{ id: "550e8400-e29b-41d4-a716-446655440001", type: "workspace" },
                subject: #{ id: "3fa85f64-5717-4562-b3fc-2c963f66afa7", type: BindingSubjectType.user },
                role: #{ id: "550e8400-e29b-41d4-a716-446655440003" }
            }
        ]
    })
    model BatchCreateRoleBindingsRequest {
        @doc("List of role bindings to create (maximum 100)")
        @minItems(1)
        @maxItems(100)
        requests: CreateRoleBindingsRequest[];
    }

    @doc("Response containing the created role bindings")
    model BatchCreateRoleBindingsResponse {
        @statusCode _: 201;
        @doc("The role bindings that were created")
        role_bindings: RoleBinding[];
    }

    @example(#{
        last_modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        subject: #{
            id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            type: "group",
            group: #{
                name: "Engineering Team",
                description: "Development and engineering team",
                user_count: 25
            }
        },
        roles: #[
            #{
                id: "550e8400-e29b-41d4-a716-446655440002",
                name: "Workspace Admin"
            }
        ],
        resource: #{
            id: "550e8400-e29b-41d4-a716-446655440001",
            name: "Engineering Workspace",
            type: "workspace"
        }
    })
    @example(#{
        last_modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
        subject: #{
            id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            type: "group",
            group: #{
                name: "Engineering Team",
                description: "Development and engineering team",
                user_count: 25
            }
        },
        roles: #[
            #{
                id: "550e8400-e29b-41d4-a716-446655440002",
                name: "Workspace Admin"
            }
        ],
        resource: #{
            id: "550e8400-e29b-41d4-a716-446655440003",
            name: "Child Workspace",
            type: "workspace"
        },
        sources: #[
            #{
                id: "550e8400-e29b-41d4-a716-446655440001",
                name: "Parent Workspace",
                type: "workspace"
            }
        ]
    })
    model RoleBindingBySubject {
        @doc("Timestamp of last modification")
        last_modified?: offsetDateTime;
        @doc("Subject of the role binding")
        subject?: Subject;
        @doc("Roles assigned to the subject")
        roles?: KesselRBAC.Role[];
        @doc("Resource the roles apply to")
        resource?: Resource;
        @doc("Resources from which this role binding is sourced (direct or inherited)")
        sources?: Resource[];
    }

    @doc("List role bindings grouped by subject. subject_type (optional) is user or group only, with subject_id as UUID. granted_subject_type, granted_subject_id, and granted_subject.principal.user_id are not query parameters on this path—use GET /role-bindings/ for those filters.")
    @summary("List role bindings grouped by subject")
    @route("by-subject/")
    @opExample(#{
        parameters: #{
            limit: 10,
            cursor: "cD0yMDI1LTA4LTE1KzE0JTNBMDQlM0E0MS42ODM3NTIlMkIwMCUzQTAw",
            resource_id: "550e8400-e29b-41d4-a716-446655440001",
            resource_type: ResourceType.workspace
        },
        returnType: #{
            _: 200,
            meta: #{
                limit: 10
            },
            links: #{
                next: "http://localhost:8000/api/rbac/v2/role-bindings/by-subject/?cursor=cD0yMDI1LTA4LTE1KzE0JTNBMDQlM0E0MS42ODM3NTIlMkIwMCUzQTAw",
                previous: null
            },
            data: #[
                #{
                    last_modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
                    subject: #{
                        id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        type: "group",
                        group: #{
                            name: "Engineering Team",
                            description: "Development and engineering team",
                            user_count: 25
                        }
                    },
                    roles: #[
                        #{
                            id: "550e8400-e29b-41d4-a716-446655440002",
                            name: "Workspace Admin"
                        }
                    ],
                    resource: #{
                        id: "550e8400-e29b-41d4-a716-446655440001",
                        name: "Engineering Workspace",
                        type: "workspace"
                    }
                }
            ]
        }
    })
    @opExample(#{
        parameters: #{
            limit: 10,
            exclude_sources: ExcludeSources.direct,
            resource_id: "550e8400-e29b-41d4-a716-446655440001",
            resource_type: ResourceType.workspace
        },
        returnType: #{
            _: 200,
            meta: #{
                limit: 10
            },
            links: #{
                next: "http://localhost:8000/api/rbac/v2/role-bindings/by-subject/?limit=10&exclude_sources=direct&cursor=xyz",
                previous: null
            },
            data: #[
                #{
                    last_modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z"),
                    subject: #{
                        id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        type: "group",
                        group: #{
                            name: "Engineering Team",
                            description: "Development and engineering team",
                            user_count: 25
                        }
                    },
                    roles: #[
                        #{
                            id: "550e8400-e29b-41d4-a716-446655440002",
                            name: "Workspace Admin"
                        }
                    ],
                    resource: #{
                        id: "550e8400-e29b-41d4-a716-446655440003",
                        name: "Child Workspace",
                        type: "workspace"
                    },
                    sources: #[
                        #{
                            id: "550e8400-e29b-41d4-a716-446655440001",
                            name: "Parent Workspace",
                            type: "workspace"
                        }
                    ]
                }
            ]
        }
    })
    @get op listBySubject(
        @query limit?: Limit = DefaultLimit;

        @doc("Cursor for cursor-based pagination.")
        @query cursor?: Cursor;

        @doc("Filter by resource ID. For workspace: UUID. For tenant: tenant resource ID (format: {domain}/{org_id}). Required unless resource.tenant.org_id is provided.")
        @example("550e8400-e29b-41d4-a716-446655440001")
        @query resource_id?: string;
        @doc("Filter by resource type. Required unless resource.tenant.org_id is provided.")
        @example(ResourceType.workspace)
        @query resource_type?: ResourceType;

        @doc("Org ID of the tenant resource to filter by. Cannot be combined with resource_id. When provided, resource_type is implicitly 'tenant'.")
        @example("12345678")
        @query("resource.tenant.org_id") resource_tenant_org_id?: string;
        @doc("Filter by binding subject kind: group or user (principal UUID). There is no granted_subject.* on this endpoint.")
        @example(BindingSubjectType.group)
        @query subject_type?: BindingSubjectType;
        @doc("Filter by subject ID (group UUID or user/principal UUID)")
        @example("550e8400-e29b-41d4-a716-446655440004")
        @query subject_id?: string;

        @query exclude_sources?: ExcludeSources = ExcludeSources.none;

        @doc("Control which fields are included in the response to optimize payload size and improve performance.")
        @example("subject(group.name,group.user_count),roles(name),resource(name,type),last_modified")
        @query fields?: FieldMask = "subject(group.name),roles(name),resource(name,type),last_modified";
        @doc("Sort by specified field(s), prefix with '-' for descending order")
        @example("subject_name,-subject_type")
        @query order_by?: OrderBy = "subject_name";
    ): ListResponse<RoleBindingBySubject> | Problems.CommonProblems;

    @doc("Grant access to a resource to a set of subjects with a set of roles")
    @summary("Grant access to a resource to a set of subjects with a set of roles")
    @route(":batchCreate/")
    @opExample(#{
        parameters: #{
            body: #{
                requests: #[
                    #{
                        resource: #{ id: "550e8400-e29b-41d4-a716-446655440001", type: "workspace" },
                        subject: #{ id: "3fa85f64-5717-4562-b3fc-2c963f66afa6", type: BindingSubjectType.group },
                        role: #{ id: "550e8400-e29b-41d4-a716-446655440002" }
                    },
                    #{
                        resource: #{ id: "550e8400-e29b-41d4-a716-446655440001", type: "workspace" },
                        subject: #{ id: "3fa85f64-5717-4562-b3fc-2c963f66afa7", type: BindingSubjectType.user },
                        role: #{ id: "550e8400-e29b-41d4-a716-446655440003" }
                    }
                ]
            }
        },
        returnType: #{
            _: 201,
            role_bindings: #[
                #{
                    resource: #{ id: "550e8400-e29b-41d4-a716-446655440001", type: "workspace" },
                    subject: #{ id: "3fa85f64-5717-4562-b3fc-2c963f66afa6", type: "group" },
                    role: #{ id: "550e8400-e29b-41d4-a716-446655440002" }
                },
                #{
                    resource: #{ id: "550e8400-e29b-41d4-a716-446655440001", type: "workspace" },
                    subject: #{ id: "3fa85f64-5717-4562-b3fc-2c963f66afa7", type: "user" },
                    role: #{ id: "550e8400-e29b-41d4-a716-446655440003" }
                }
            ]
        }
    })
    @post op batchCreate(
        @query fields?: FieldMask = "resource(id,type),role(id),subject(id,type)";
        @body body: BatchCreateRoleBindingsRequest;
    ): BatchCreateRoleBindingsResponse | Problems.CommonProblems;

    @doc("Request body for updating role bindings - contains the new roles to assign")
    @example(#{
        roles: #[
            #{ id: "550e8400-e29b-41d4-a716-446655440002" },
            #{ id: "550e8400-e29b-41d4-a716-446655440003" }
        ]
    })
    model UpdateRoleBindingsRequest {
        @doc("Roles to assign (replaces existing roles for the target binding). Empty array removes all bindings.")
        roles: {
            @doc("Role identifier")
            id: UUID;
        }[];
    }

    @doc("Update roles for a specific subject on a resource. Replaces all existing roles with the provided roles. subject_type is user or group only (UUID via subject_id). granted_subject.* query params are not supported here; use GET /role-bindings/ to filter by external user ID.")
    @summary("Update role bindings")
    @route("by-subject/")
    @opExample(#{
        parameters: #{
            resource_id: "550e8400-e29b-41d4-a716-446655440001",
            resource_type: ResourceType.workspace,
            subject_id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            subject_type: BindingSubjectType.group,
            body: #{
                roles: #[
                    #{ id: "550e8400-e29b-41d4-a716-446655440002" },
                    #{ id: "550e8400-e29b-41d4-a716-446655440003" }
                ]
            }
        },
        returnType: #{
            _: 200,
            subject: #{ id: "3fa85f64-5717-4562-b3fc-2c963f66afa6", type: "group", group: #{ name: "Engineering Team", user_count: 25 } },
            roles: #[
                #{ id: "550e8400-e29b-41d4-a716-446655440002", name: "Workspace Admin" },
                #{ id: "550e8400-e29b-41d4-a716-446655440003", name: "Viewer" }
            ],
            resource: #{ id: "550e8400-e29b-41d4-a716-446655440001", type: "workspace", name: "Engineering Workspace" }
        }
    })
    @put op update(
        @doc("Identify the resource ID for the set of role bindings to replace. For workspace: UUID. For tenant: tenant resource ID (format: {domain}/{org_id}). Required unless resource.tenant.org_id is provided.")
        @example("550e8400-e29b-41d4-a716-446655440001")
        @query resource_id?: string;

        @doc("Identify the resource type for the set of role bindings to replace. Required unless resource.tenant.org_id is provided.")
        @example(ResourceType.workspace)
        @query resource_type?: ResourceType;

        @doc("Org ID of the tenant resource. Cannot be combined with resource_id. When provided, resource_type is implicitly 'tenant'.")
        @example("12345678")
        @query("resource.tenant.org_id") resource_tenant_org_id?: string;

        @doc("Identify the subject ID for the set of role bindings to replace")
        @example("550e8400-e29b-41d4-a716-446655440004")
        @query subject_id: string;

        @doc("Subject kind: group or user (principal UUID). No granted_subject.* params on this path.")
        @example(BindingSubjectType.group)
        @query subject_type: BindingSubjectType;

        @body body: UpdateRoleBindingsRequest;

        @query fields?: FieldMask = DefaultRoleBindingBySubjectFieldMask;
    ): ItemResponse<RoleBindingBySubject, 200> | Problems.CommonProblems | Problems.Problem404;

    @example(#{
        role: #{
            id: "550e8400-e29b-41d4-a716-446655440002",
            name: "Workspace Admin",
            description: "Full administrative access to workspace",
            permissions: #[
                #{
                    application: "rbac",
                    resource_type: "workspace",
                    operation: "read"
                },
                #{
                    application: "rbac",
                    resource_type: "workspace",
                    operation: "write"
                }
            ]
        },
        subject: #{
            id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            type: "group",
            group: #{
                name: "Engineering Team",
                description: "Development and engineering team",
                user_count: 25
            }
        }
    })
    @example(#{
        role: #{
            id: "550e8400-e29b-41d4-a716-446655440002",
            name: "Workspace Admin",
            permissions: #[
                #{
                    application: "rbac",
                    resource_type: "workspace",
                    operation: "read"
                },
                #{
                    application: "rbac",
                    resource_type: "workspace",
                    operation: "write"
                }
            ]
        },
        subject: #{
            id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            type: "group",
            group: #{
                name: "Engineering Team",
                user_count: 25
            }
        }
    })
    model RoleBinding {
        @doc("The role that grants permissions to the subject on the resource")
        role?: KesselRBAC.Role;
        @doc("The subject (user or group) that is granted the role")
        subject?: Subject;
        @doc("The resource on which the subject is granted the role")
        resource?: Resource;
        @doc("Resources from which this role binding is sourced (direct or inherited)")
        sources?: Resource[];
    }

    @doc("List role bindings")
    @summary("List role bindings")
    @opExample(#{
        parameters: #{
            limit: 10
        },
        returnType: #{
            _: 200,
            meta: #{
                limit: 10
            },
            links: #{
                next: "http://localhost:8000/api/rbac/v2/role-bindings/?cursor=cD0yMDI1LTA4LTE1",
                previous: null
            },
            data: #[
                #{
                    role: #{
                        id: "550e8400-e29b-41d4-a716-446655440002"
                    },
                    subject: #{
                        id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        type: "group"
                    },
                    resource: #{
                        id: "550e8400-e29b-41d4-a716-446655440001"
                    },
                    sources: #[
                        #{
                            id: "550e8400-e29b-41d4-a716-446655440001"
                        }
                    ]
                }
            ]
        }
    })
    @get op list(
        @query limit?: Limit = DefaultLimit;

        @doc("Cursor for cursor-based pagination.")
        @query cursor?: Cursor;

        @doc("Filter by role ID")
        @example("550e8400-e29b-41d4-a716-446655440002")
        @query role_id?: string;

        @doc("Filter by resource ID")
        @example("550e8400-e29b-41d4-a716-446655440001")
        @query resource_id?: string;

        @doc("Filter by resource type")
        @example("workspace")
        @query resource_type?: string;

        @doc("Org ID of the tenant resource to filter by. Cannot be combined with resource_id. When provided, resource_type is implicitly 'tenant'.")
        @example("12345678")
        @query("resource.tenant.org_id") resource_tenant_org_id?: string;

        @doc("Filter by binding subject type (group or user) on each row. Distinct from granted_subject_type.")
        @example(BindingSubjectType.group)
        @query subject_type?: BindingSubjectType;

        @doc("Filter by subject ID")
        @example("3fa85f64-5717-4562-b3fc-2c963f66afa6")
        @query subject_id?: string;

        @query exclude_sources?: ExcludeSources = ExcludeSources.none;

        @doc("Filter by the type of subject effectively granted access. When 'user', returns role bindings granted directly to the user and through their group memberships. When 'group', returns role bindings granted to the group. When 'principal', filters by external user ID via granted_subject.principal.user_id. Cannot be combined with subject_type/subject_id.")
        @example(GrantedSubjectFilterType.user)
        @query granted_subject_type?: GrantedSubjectFilterType;

        @doc("ID of the subject effectively granted access. Accepts a principal UUID or group UUID. Required when granted_subject_type is 'user' or 'group'.")
        @example("3fa85f64-5717-4562-b3fc-2c963f66afa6")
        @query granted_subject_id?: string;

        @doc("External user ID of the principal effectively granted access. Required when granted_subject_type is 'principal'.")
        @example("jsmith")
        @query("granted_subject.principal.user_id") granted_subject_principal_user_id?: string;

        @doc("Control which fields are included in the response to optimize payload size.")
        @query fields?: FieldMask = "resource(id),role(id),subject(id,type),sources(id)";

        @doc("Default sort is by the time the role was first created. Prefix with '-' for descending order.")
        @query order_by?: OrderBy = "role.id";

    ): ListResponse<RoleBinding> | Problems.CommonProblems;
}

@route("/roles/")
@tag("Roles")
@doc("Operations about roles")
namespace Roles {

    /**
     * Default fields returned for role detail operations (get/create/update)
     */
    const DefaultRoleFieldMask: FieldMask = "id,name,description,permissions,last_modified";

    @doc("Role data for create and update operations")
    @example(#{
        name: "Host Inventory Admin",
        description: "Custom role for host inventory management",
        permissions: #[
            #{ application: "inventory", resource_type: "hosts", operation: "read" },
            #{ application: "inventory", resource_type: "hosts", operation: "write" }
        ]
    })
    model CreateOrUpdateRoleRequest {
        @doc("A human readable name for the role. Must be unique within a tenant. Asterisks (*) are not allowed.")
        @pattern("^[^*]+$")
        name: string;
        @doc("A description of the role to help clarify its purpose. Does not need to be unique.")
        description?: string;
        @doc("List of permissions to assign to this role")
        permissions: Permission[];
    }

    model RoleResponse<StatusCode extends int32> {
        @statusCode _: StatusCode;
        ...KesselRBAC.Role;
    }

    @doc("List the roles for a tenant")
    @summary("List the roles for a tenant")
    @opExample(#{
        parameters: #{
            limit: 10
        },
        returnType: #{
            _: 200,
            meta: #{
                limit: 10
            },
            links: #{
                next: "/api/rbac/v2/roles/?limit=10&cursor=eyJpZCI6IjEyMyJ9",
                previous: null
            },
            data: #[
                #{
                    id: "550e8400-e29b-41d4-a716-446655440002",
                    name: "Workspace Admin",
                    description: "description",
                    last_modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
                }
            ]
        }
    })
    @opExample(#{
        parameters: #{
            limit: 10,
            fields: "id,name,permissions_count"
        },
        returnType: #{
            _: 200,
            meta: #{
                limit: 10
            },
            links: #{
                next: "/api/rbac/v2/roles/?limit=10&fields=id,name,permissions_count&cursor=eyJpZCI6IjEyMyJ9",
                previous: null
            },
            data: #[
                #{
                    id: "550e8400-e29b-41d4-a716-446655440002",
                    name: "Workspace Admin",
                    permissions_count: 2
                }
            ]
        }
    })
    @get op list(
        @query limit?: Limit = DefaultLimit;

        @doc("Cursor for cursor-based pagination.")
        @query cursor?: Cursor;

        @doc("Filter by role name. Case-insensitive substring match by default; use * for glob patterns (e.g. foo*).")
        @query name?: string;

        @doc("Filter roles by the resource type (e.g. 'tenant', 'workspace'). When omitted, returns roles from all scopes.")
        @query resource_type?: string;

        @doc("Filter by resource ID. For workspace: UUID. For tenant: tenant resource ID (format: {domain}/{org_id}). Requires resource_type unless resource.tenant.org_id is provided.")
        @query resource_id?: string;

        @doc("Org ID of the tenant resource to filter by. Cannot be combined with resource_id. When provided, resource_type is implicitly 'tenant'.")
        @query("resource.tenant.org_id") resource_tenant_org_id?: string;

        @doc("Control which fields are included in the response to optimize payload size.")
        @query fields?: FieldMask = "id,name,description,last_modified";

        @doc("Sort by specified field(s), prefix with '-' for descending order. Allowed fields: name, last_modified.")
        @query order_by?: OrderBy = "name";
    ): ListResponse<Role> | Problems.CommonProblems;


    @doc("Create a custom role in tenant")
    @summary("Create a custom role in tenant")
    @opExample(#{
        parameters: #{
            body: #{
                name: "Custom Inventory Admin",
                description: "Custom role for inventory management",
                permissions: #[
                    #{ application: "inventory", resource_type: "hosts", operation: "read" },
                    #{ application: "inventory", resource_type: "hosts", operation: "write" }
                ]
            }
        },
        returnType: #{
            _: 201,
            id: "550e8400-e29b-41d4-a716-446655440002",
            name: "Custom Inventory Admin",
            description: "Custom role for inventory management",
            permissions: #[
                #{ application: "inventory", resource_type: "hosts", operation: "read" },
                #{ application: "inventory", resource_type: "hosts", operation: "write" }
            ],
            last_modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
        }
    })
    @post op create(
        @body body: CreateOrUpdateRoleRequest;

        @doc("""
        Control which fields are included in the response to optimize payload size.

        **When fields parameter is provided:**
        Only explicitly specified fields are returned. All response fields become optional.

        **Examples:**
        - `?fields=id,name,permissions_count` - Include id, name, and count (no permissions array)
        - `?fields=permissions_count,last_modified` - Include only count and timestamp
        - `?fields=id,name,org_id` - Include org_id (not returned by default; must be requested explicitly)
        """)
        @example("id,name,permissions_count")
        @query fields?: string = DefaultRoleFieldMask;
    ): ItemResponse<Role, 201> | Problems.CommonProblems;



    @route("{id}/")
    @opExample(#{
        parameters: #{
            id: "550e8400-e29b-41d4-a716-446655440002"
        },
        returnType: #{
            _: 200,
            id: "550e8400-e29b-41d4-a716-446655440002",
            name: "Workspace Admin",
            description: "Full administrative access to workspace resources",
            permissions: #[
                #{ application: "inventory", resource_type: "hosts", operation: "read" },
                #{ application: "inventory", resource_type: "hosts", operation: "write" }
            ],
            last_modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
        }
    })
    @opExample(#{
        parameters: #{
            id: "550e8400-e29b-41d4-a716-446655440002",
            fields: "name"
        },
        returnType: #{
            _: 200,
            name: "Workspace Admin"
        }
    })
    @get op read(
        @path id: UUID;

        @doc("Control which fields are included in the response to optimize payload size.")
        @query fields?: FieldMask = DefaultRoleFieldMask;
    ): ItemResponse<Role, 200> | Problems.CommonProblems | Problems.Problem404;

    @doc("Update a custom role in tenant")
    @summary("Update a custom role in tenant")
    @route("{id}/")
    @opExample(#{
        parameters: #{
            id: "550e8400-e29b-41d4-a716-446655440002",
            body: #{
                name: "Updated Inventory Admin",
                description: "Updated role for inventory management",
                permissions: #[
                    #{ application: "inventory", resource_type: "hosts", operation: "read" },
                    #{ application: "inventory", resource_type: "hosts", operation: "write" },
                    #{ application: "inventory", resource_type: "hosts", operation: "delete" }
                ]
            }
        },
        returnType: #{
            _: 200,
            id: "550e8400-e29b-41d4-a716-446655440002",
            name: "Updated Inventory Admin",
            description: "Updated role for inventory management",
            permissions: #[
                #{ application: "inventory", resource_type: "hosts", operation: "read" },
                #{ application: "inventory", resource_type: "hosts", operation: "write" },
                #{ application: "inventory", resource_type: "hosts", operation: "delete" }
            ],
            last_modified: offsetDateTime.fromISO("2024-08-04T12:00:00Z")
        }
    })
    @put op update(
        @doc("UUID that uniquely identifies the role")
        @path id: UUID;
        @body body: CreateOrUpdateRoleRequest;

        @doc("Control which fields are included in the response to optimize payload size.")
        @query fields?: FieldMask = DefaultRoleFieldMask;
    ): ItemResponse<Role, 200> | Problems.CommonProblems | Problems.Problem404;

    @doc("Data for request to delete multiple roles by ID.")
    model BatchDeleteRolesRequest {
        @doc("The IDs of the roles to delete. A maximum of 100 roles can be deleted in one batch.")
        @minItems(1)
        @maxItems(100)
        @example(#["3a780e8d-da23-4a2c-a457-c9d31d8a6df0", "4fc3eb8e-2675-4807-8072-c69bd2a66482"])
        ids: UUID[];
    }

    @example(#{ statusCode: 204 })
    model BatchDeleteRolesResponse {
        @statusCode
        statusCode: 204,
    }

    @doc("Delete multiple roles by ID atomically.")
    @route(":batchDelete/")
    @opExample(#{
        parameters: #{
            body: #{
                ids: #["3a780e8d-da23-4a2c-a457-c9d31d8a6df0", "4fc3eb8e-2675-4807-8072-c69bd2a66482"],
            }
        },
        returnType: #{
            statusCode: 204
        }
    })
    @post op batchDelete(
        @body body: BatchDeleteRolesRequest
    ): BatchDeleteRolesResponse | Problems.CommonProblems | Problems.Problem404;
}
