from collections import defaultdict
from datetime import datetime
from itertools import groupby
from operator import itemgetter

import dpath
from mongoengine import Q

import database
from apierrors import errors
from apimodels.base import UpdateResponse
from bll.task import TaskBLL
from database.errors import translate_errors_context
from database.model.model import Model
from database.model.project import Project
from database.model.task.task import Task, TaskStatus, TaskVisibility
from database.utils import parse_from_call, get_options, get_company_or_none_constraint
from service_repo import APICall, endpoint
from timing_context import TimingContext

task_bll = TaskBLL()
archived_tasks_cond = {"$in": [TaskVisibility.archived.value, "$tags"]}

create_fields = {
    "name": None,
    "description": None,
    "tags": list,
    "default_output_destination": None,
}

get_all_query_options = Project.QueryParameterOptions(
    pattern_fields=("name", "description"), list_fields=("tags", "id")
)


@endpoint("projects.get_by_id", required_fields=["project"])
def get_by_id(call):
    assert isinstance(call, APICall)
    project_id = call.data["project"]

    with translate_errors_context():
        with TimingContext("mongo", "projects_by_id"):
            query = Q(id=project_id) & get_company_or_none_constraint(
                call.identity.company
            )
            res = Project.objects(query).first()
        if not res:
            raise errors.bad_request.InvalidProjectId(id=project_id)

        res = res.to_proper_dict()

        call.result.data = {"project": res}


def make_projects_get_all_pipelines(project_ids, specific_state=None):
    archived = TaskVisibility.archived.value
    status_count_pipeline = [
        # count tasks per project per status
        {"$match": {"project": {"$in": project_ids}}},
        # make sure tags is always an array (required by subsequent $in in archived_tasks_cond)
        {
            "$addFields": {
                "tags": {
                    "$cond": {
                        "if": {"$ne": [{"$type": "$tags"}, "array"]},
                        "then": [],
                        "else": "$tags",
                    }
                }
            }
        },
        {
            "$group": {
                "_id": {
                    "project": "$project",
                    "status": "$status",
                    archived: archived_tasks_cond,
                },
                "count": {"$sum": 1},
            }
        },
        # for each project, create a list of (status, count, archived)
        {
            "$group": {
                "_id": "$_id.project",
                "counts": {
                    "$push": {
                        "status": "$_id.status",
                        "count": "$count",
                        archived: "$_id.%s" % archived,
                    }
                },
            }
        },
    ]

    def runtime_subquery(additional_cond):
        return {
            # the sum of
            "$sum": {
                # for each task
                "$cond": {
                    # if completed and started and completed > started
                    "if": {
                        "$and": [
                            "$started",
                            "$completed",
                            {"$gt": ["$completed", "$started"]},
                            additional_cond,
                        ]
                    },
                    # then: floor((completed - started) / 1000)
                    "then": {
                        "$floor": {
                            "$divide": [
                                {"$subtract": ["$completed", "$started"]},
                                1000.0,
                            ]
                        }
                    },
                    "else": 0,
                }
            }
        }

    group_step = {"_id": "$project"}

    for state in TaskVisibility:
        if specific_state and state != specific_state:
            continue
        if state == TaskVisibility.active:
            group_step[state.value] = runtime_subquery({"$not": archived_tasks_cond})
        elif state == TaskVisibility.archived:
            group_step[state.value] = runtime_subquery(archived_tasks_cond)

    runtime_pipeline = [
        # only count run time for these types of tasks
        {
            "$match": {
                "type": {"$in": ["training", "testing", "annotation"]},
                "project": {"$in": project_ids},
            }
        },
        {
            # for each project
            "$group": group_step
        },
    ]

    return status_count_pipeline, runtime_pipeline


@endpoint("projects.get_all_ex")
def get_all_ex(call):
    assert isinstance(call, APICall)
    include_stats = call.data.get("include_stats")
    stats_for_state = call.data.get("stats_for_state", TaskVisibility.active.value)

    if stats_for_state:
        try:
            specific_state = TaskVisibility(stats_for_state)
        except ValueError:
            raise errors.bad_request.FieldsValueError(stats_for_state=stats_for_state)
    else:
        specific_state = None

    with translate_errors_context(), TimingContext("mongo", "projects_get_all"):
        res = Project.get_many_with_join(
            company=call.identity.company,
            query_dict=call.data,
            query_options=get_all_query_options,
            allow_public=True,
        )

        if not include_stats:
            call.result.data = {"projects": res}
            return

        ids = [project["id"] for project in res]
        status_count_pipeline, runtime_pipeline = make_projects_get_all_pipelines(
            ids, specific_state=specific_state
        )

        default_counts = dict.fromkeys(get_options(TaskStatus), 0)

        def set_default_count(entry):
            return dict(default_counts, **entry)

        status_count = defaultdict(lambda: {})
        key = itemgetter(TaskVisibility.archived.value)
        for result in Task.objects.aggregate(*status_count_pipeline):
            for k, group in groupby(sorted(result["counts"], key=key), key):
                section = (
                    TaskVisibility.archived if k else TaskVisibility.active
                ).value
                status_count[result["_id"]][section] = set_default_count(
                    {
                        count_entry["status"]: count_entry["count"]
                        for count_entry in group
                    }
                )

        runtime = {
            result["_id"]: {k: v for k, v in result.items() if k != "_id"}
            for result in Task.objects.aggregate(*runtime_pipeline)
        }

    def safe_get(obj, path, default=None):
        try:
            return dpath.get(obj, path)
        except KeyError:
            return default

    def get_status_counts(project_id, section):
        path = "/".join((project_id, section))
        return {
            "total_runtime": safe_get(runtime, path, 0),
            "status_count": safe_get(status_count, path, default_counts),
        }

    report_for_states = [
        s for s in TaskVisibility if not specific_state or specific_state == s
    ]

    for project in res:
        project["stats"] = {
            task_state.value: get_status_counts(project["id"], task_state.value)
            for task_state in report_for_states
        }

    call.result.data = {"projects": res}


@endpoint("projects.get_all")
def get_all(call):
    assert isinstance(call, APICall)

    with translate_errors_context(), TimingContext("mongo", "projects_get_all"):
        res = Project.get_many(
            company=call.identity.company,
            query_dict=call.data,
            query_options=get_all_query_options,
            parameters=call.data,
            allow_public=True,
        )

        call.result.data = {"projects": res}


@endpoint("projects.create", required_fields=["name", "description"])
def create(call):
    assert isinstance(call, APICall)
    identity = call.identity

    with translate_errors_context():
        fields = parse_from_call(call.data, create_fields, Project.get_fields())
        now = datetime.utcnow()
        project = Project(
            id=database.utils.id(),
            user=identity.user,
            company=identity.company,
            created=now,
            last_update=now,
            **fields
        )
        with TimingContext("mongo", "projects_save"):
            project.save()
        call.result.data = {"id": project.id}


@endpoint(
    "projects.update", required_fields=["project"], response_data_model=UpdateResponse
)
def update(call):
    """
    update

    :summary: Update project information.
              See `project.create` for parameters.
    :return: updated - `int` - number of projects updated
             fields - `[string]` - updated fields
    """
    assert isinstance(call, APICall)
    project_id = call.data["project"]

    with translate_errors_context():
        project = Project.get_for_writing(company=call.identity.company, id=project_id)
        if not project:
            raise errors.bad_request.InvalidProjectId(id=project_id)

        fields = parse_from_call(
            call.data, create_fields, Project.get_fields(), discard_none_values=False
        )
        fields["last_update"] = datetime.utcnow()
        with TimingContext("mongo", "projects_update"):
            updated = project.update(upsert=False, **fields)
        call.result.data_model = UpdateResponse(updated=updated, fields=fields)


@endpoint("projects.delete", required_fields=["project"])
def delete(call):
    assert isinstance(call, APICall)
    project_id = call.data["project"]
    force = call.data.get("force", False)

    with translate_errors_context():
        project = Project.get_for_writing(company=call.identity.company, id=project_id)
        if not project:
            raise errors.bad_request.InvalidProjectId(id=project_id)

        # NOTE: from this point on we'll use the project ID and won't check for company, since we assume we already
        # have the correct project ID.

        # Find the tasks which belong to the project
        for cls, error in (
            (Task, errors.bad_request.ProjectHasTasks),
            (Model, errors.bad_request.ProjectHasModels),
        ):
            res = cls.objects(
                project=project_id, tags__nin=[TaskVisibility.archived.value]
            ).only("id")
            if res and not force:
                raise error("use force=true to delete", id=project_id)

        updated_count = res.update(project=None)

        project.delete()

        call.result.data = {"deleted": 1, "disassociated_tasks": updated_count}


@endpoint("projects.get_unique_metric_variants")
def get_unique_metric_variants(call, company_id, req_model):
    project_id = call.data.get("project")

    metrics = task_bll.get_unique_metric_variants(
        company_id, [project_id] if project_id else None
    )

    call.result.data = {"metrics": metrics}
