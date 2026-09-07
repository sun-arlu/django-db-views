from typing import Type

import django
import six
import sqlparse
from django.apps import apps
from django.conf import settings
from django.db import connection, models, ProgrammingError
from django.db.migrations import SeparateDatabaseAndState
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.graph import MigrationGraph

if django.VERSION >= (5, 1):
    from django.db.migrations.autodetector import OperationDependency

from django_db_views.db_view import DBMaterializedView, DBView, DBViewsRegistry
from django_db_views.operations import (
    AddFieldComment,
    AlterFieldComment,
    DBViewModelState,
    ViewDropRunPython,
    ViewRunPython, RemoveFieldComment,
)
from django_db_views.migration_functions import (
    BackwardMaterializedViewMigration,
    BackwardViewMigration,
    BackwardViewMigrationBase,
    DropMaterializedView,
    DropView,
    DropViewMigration,
    ForwardMaterializedViewMigration,
    ForwardViewMigration,
    ForwardViewMigrationBase,
)


class ViewMigrationAutoDetector(MigrationAutodetector):
    """
    We have overwritten only the `_detect_changes` function.
    It's almost the same code as in the regular function.
    We just removed generating other operations, and instead of them added our detection.
    Other methods are our code that we use for detection.
    It's detecting only view model changes and comment changes.
    """

    def _detect_changes(self, convert_apps=None, graph=None) -> dict:
        # <START copy paste from MigrationAutodetector, depends on django version>
        if django.VERSION >= (4,):
            self._detect_changes_preparation_django_version_4_and_above(convert_apps)
        else:
            raise Exception("Django version must be >= 4")

        # Renames have to come first
        self.generate_renamed_models()

        # Prepare lists of fields and generate through model map
        self._prepare_field_lists()
        self._generate_through_model_map()

        # Create the renamed fields and store them in self.renamed_fields.
        # They are used by create_altered_indexes(), generate_altered_fields(),
        # generate_removed_altered_index/unique_together(), and
        # generate_altered_index/unique_together().
        self.create_renamed_fields()

        # Generate field operations.
        self.generate_removed_fields()
        # <END of copy paste from MigrationAutodetector>

        # Generate view operations.
        self.generate_views_operations(graph)
        self.delete_old_views()

        # <START copy paste from MigrationAutodetector>
        # Generate field operations.
        self.generate_added_fields()
        self.generate_altered_fields()

        # Use indices because they are much simpler.
        self.old_indexes = set()
        self.new_indexes = set()
        self.detect_index_changes()
        self.drop_indexes()
        self.generate_indexes()

        self._sort_migrations()
        self._build_migration_list(graph)
        self._optimize_migrations()

        return self.migrations
        # <END end of copy paste from MigrationAutodetector>

    def _detect_changes_preparation_django_version_4_and_above(self, convert_apps):
        self.generated_operations = {}
        self.altered_indexes = {}
        self.altered_constraints = {}
        self.renamed_fields = {}

        # Prepare some old/new state and model lists, separating
        # proxy models and ignoring unmigrated apps.
        self.old_model_keys = set()
        self.old_proxy_keys = set()
        self.old_unmanaged_keys = set()
        self.new_model_keys = set()
        self.new_proxy_keys = set()
        self.new_unmanaged_keys = set()
        for (app_label, model_name), model_state in self.from_state.models.items():
            if not model_state.options.get("managed", True):
                self.old_unmanaged_keys.add((app_label, model_name))
            # elif app_label not in self.from_state.real_apps:
            #     if model_state.options.get("proxy"):
            #         self.old_proxy_keys.add((app_label, model_name))
            #     else:
            #         self.old_model_keys.add((app_label, model_name))

        for (app_label, model_name), model_state in self.to_state.models.items():
            if not model_state.options.get("managed", True):
                self.new_unmanaged_keys.add((app_label, model_name))
            # elif app_label not in self.from_state.real_apps or (
            #     convert_apps and app_label in convert_apps
            # ):
            #     if model_state.options.get("proxy"):
            #         self.new_proxy_keys.add((app_label, model_name))
            #     else:
            #         self.new_model_keys.add((app_label, model_name))

        self.from_state.resolve_fields_and_relations()
        self.to_state.resolve_fields_and_relations()

    def _prepare_field_lists(self):
        """
        Prepare field lists and a list of the fields that used through models
        in the old state so dependencies can be made from the through model
        deletion to the field that uses it.
        """
        self.kept_model_keys = self.old_model_keys & self.new_model_keys
        self.kept_proxy_keys = self.old_proxy_keys & self.new_proxy_keys
        self.kept_unmanaged_keys = self.old_unmanaged_keys & self.new_unmanaged_keys
        self.through_users = {}
        self.old_field_keys = {
            (app_label, model_name, field_name)
            for app_label, model_name in self.kept_unmanaged_keys
            for field_name in self.from_state.models[
                app_label, self.renamed_models.get((app_label, model_name), model_name),
            ].fields
        }
        self.new_field_keys = {
            (app_label, model_name, field_name)
            for app_label, model_name in self.kept_unmanaged_keys
            for field_name in self.to_state.models[app_label, model_name].fields
        }

    def delete_old_views(self):
        for (app_label, table_name), model_state in self.get_previous_view_models_state().items():
            if model_state.table_name not in DBViewsRegistry:
                self.add_operation(
                    app_label,
                    ViewDropRunPython(
                        self.get_drop_migration_class(model_state.base_class)(
                            model_state.table_name, engine=model_state.view_engine
                        ),
                        self.get_backward_migration_class(model_state.base_class)(
                            model_state.view_definition,
                            model_state.table_name,
                            engine=model_state.view_engine,
                        ),
                        atomic=False,
                    ),
                )

    def get_previous_view_models_state(self) -> dict:
        view_models = {}
        for (app_label, table_name), model_state in self.from_state.models.items():
            if isinstance(model_state, DBViewModelState):
                key = (app_label, table_name)
                view_models[key] = model_state
        return view_models

    def get_current_view_models_state(self) -> dict:
        view_models = {}
        for (app_label, table_name), model_state in self.to_state.models.items():
            if isinstance(model_state, DBViewModelState):
                key = (app_label, table_name)
                view_models[key] = model_state
        return view_models

    @staticmethod
    def get_current_view_models():
        view_models = {}
        for app_label, models in apps.all_models.items():
            for model_name, model_class in models.items():
                if model_class._meta.db_table in DBViewsRegistry:
                    key = (app_label, model_name)
                    view_models[key] = model_class
        return view_models

    @staticmethod
    def is_same_views(current: str, new: str) -> bool:
        def sql_normalize(s: str) -> str:
            return sqlparse.format(
                s,
                compact=True,
                keyword_case="upper",
                identifier_case="lower",
                reindent=True,
                strip_comments=True,
            ).strip()

        return sql_normalize(current) == sql_normalize(new)

    def generate_views_operations(self, graph: MigrationGraph) -> None:
        view_models = self.get_current_view_models()
        for (app_label, model_name), view_model in view_models.items():
            new_view_definition = self.get_view_definition_from_model(view_model)
            for engine, latest_view_definition in new_view_definition.items():
                current_view_definition = self.get_previous_view_definition_state(
                    graph, app_label, view_model._meta.db_table, engine
                )
                if not self.is_same_views(
                    current_view_definition, latest_view_definition
                ):
                    # Depend on all bases
                    model_state = self.to_state.models[app_label, model_name]
                    dependencies = []
                    for base in model_state.bases:
                        if isinstance(base, six.string_types) and "." in base:
                            base_app_label, base_name = base.split(".", 1)
                            if django.VERSION >= (5, 1):
                                dependency = OperationDependency(
                                    base_app_label,
                                    base_name,
                                    None,
                                    OperationDependency.Type.CREATE,
                                )
                            else:
                                dependency = (base_app_label, base_name, None, True)
                            dependencies.append(dependency)
                    dependencies += getattr(view_model, "dependencies", [])
                    self.add_operation(
                        app_label,
                        ViewRunPython(
                            self.get_forward_migration_class(view_model)(
                                latest_view_definition.strip(";"),
                                view_model._meta.db_table,
                                engine=engine,
                            ),
                            self.get_backward_migration_class(view_model)(
                                current_view_definition.strip(";"),
                                view_model._meta.db_table,
                                engine=engine,
                            ),
                            atomic=False,
                        ),
                        dependencies=dependencies,
                    )
                    # Also generate AddFieldComment for all fields of that table.
                    # This ensures field comments are (re)applied whenever the view is (re)created.
                    for field_name in self.to_state.models[app_label, model_name].fields:
                        self._generate_added_field(app_label, model_name, field_name)

    @staticmethod
    def get_forward_migration_class(model) -> Type[ForwardViewMigrationBase]:
        if issubclass(model, DBMaterializedView):
            return ForwardMaterializedViewMigration
        if issubclass(model, DBView):
            return ForwardViewMigration
        else:
            raise NotImplementedError

    @staticmethod
    def get_backward_migration_class(model) -> Type[BackwardViewMigrationBase]:
        if issubclass(model, DBMaterializedView):
            return BackwardMaterializedViewMigration
        if issubclass(model, DBView):
            return BackwardViewMigration
        else:
            raise NotImplementedError

    def get_drop_migration_class(self, model) -> Type[DropViewMigration]:
        if issubclass(model, DBMaterializedView):
            return DropMaterializedView
        elif issubclass(model, DBView):
            return DropView
        else:
            raise NotImplementedError

    @classmethod
    def get_view_definition_from_model(cls, view_model: DBView) -> dict:
        view_definitions = {}
        if callable(view_model.view_definition):
            raw_view_definition = view_model.view_definition()
        else:
            raw_view_definition = view_model.view_definition

        if isinstance(raw_view_definition, dict):
            for engine, definition in raw_view_definition.items():
                view_definitions[engine] = cls.get_cleaned_view_definition_value(
                    definition
                )
        else:
            engine = settings.DATABASES["default"]["ENGINE"]
            view_definitions[engine] = cls.get_cleaned_view_definition_value(
                raw_view_definition
            )
        return view_definitions

    def get_previous_view_definition_state(
        self, graph: MigrationGraph, app_label: str, for_table_name: str, engine: str,
    ) -> str:
        nodes = graph.leaf_nodes(app_label)
        last_node = nodes[0] if nodes else None

        while last_node:
            migration = graph.nodes[last_node]
            if migration.operations:
                for operation in migration.operations:
                    if isinstance(operation, ViewRunPython):
                        (
                            table_name,
                            previous_view_engine,
                        ) = self._get_view_identifiers_from_operation(operation)
                        if (
                            table_name == for_table_name
                            and previous_view_engine == engine
                        ):
                            return operation.code.view_definition.strip()
                    elif isinstance(operation, SeparateDatabaseAndState):
                        view_operations = list(
                            filter(
                                lambda op: isinstance(op, ViewRunPython),
                                operation.database_operations,
                            )
                        )
                        if view_operations:
                            assert (
                                len(view_operations) <= 1
                            ), ("SeparateDatabaseAndState can't contain more than one "
                                "ViewRunPython operation")
                            view_operation = view_operations[0]
                            (
                                table_name,
                                previous_view_engine,
                            ) = self._get_view_identifiers_from_operation(
                                view_operation
                            )
                            if (
                                table_name == for_table_name
                                and previous_view_engine == engine
                            ):
                                return view_operation.code.view_definition.strip()
            # right now i get only migrations from the same app.
            app_parents = list(
                sorted(
                    filter(
                        lambda x: x[0] == app_label, graph.node_map[last_node].parents
                    )
                )
            )
            if app_parents:
                last_node = app_parents[-1]
            else:  # if no parents mean we found initial migration
                last_node = None
        return ""

    def _get_view_identifiers_from_operation(self, operation) -> tuple[str, str]:
        table_name = operation.code.table_name
        if hasattr(operation.code, "view_engine") and operation.code.view_engine:
            engine = operation.code.view_engine
        else:
            engine = settings.DATABASES["default"]["ENGINE"]
        return table_name, engine

    @staticmethod
    def get_cleaned_view_definition_value(view_definition: str) -> str:
        assert isinstance(
            view_definition, str
        ), "View definition must be callable and return string or be itself a string."
        return view_definition.strip()

    def get_current_view_definition_from_database(self, table_name: str) -> str:
        """working only with postgres"""
        with connection.cursor() as cursor:
            try:
                cursor.execute("SELECT pg_get_viewdef('%s')" % table_name)
                current_view_definition = cursor.fetchone()[0].strip()
            except ProgrammingError:  # VIEW NOT EXIST
                current_view_definition = ""
            finally:
                return current_view_definition

    def _generate_added_field(self, app_label, model_name, field_name):
        # <START copy paste from MigrationAutodetector>
        field = self.to_state.models[app_label, model_name].get_field(field_name)
        # Adding a field always depends at least on its removal.
        # <END of copy paste from MigrationAutodetector>
        if django.VERSION >= (5, 1):
            dependencies = [
                OperationDependency(
                    app_label,
                    model_name,
                    field_name,
                    OperationDependency.Type.REMOVE,
                )
            ]
        else:
            dependencies = [(app_label, model_name, field_name, False)]
        # <START copy paste from MigrationAutodetector>
        # Fields that are foreignkeys/m2ms depend on stuff.
        if field.remote_field and field.remote_field.model:
            dependencies.extend(
                self._get_dependencies_for_foreign_key(
                    app_label,
                    model_name,
                    field,
                    self.to_state,
                )
            )
        # <END of copy paste from MigrationAutodetector>

        # Avoid duplicates: skip if AddFieldComment already scheduled for this field
        scheduled_ops = self.generated_operations.get(app_label, [])
        if any(
            isinstance(op, AddFieldComment)
            and op.model_name == model_name
            and op.name == field_name
            for op in scheduled_ops
        ):
            return

        self.add_operation(
            app_label,
            AddFieldComment(
                model_name=model_name,
                name=field_name,
                field=field,
            ),
            dependencies=dependencies,
        )

    def _generate_removed_field(self, app_label, model_name, field_name):

        if django.VERSION >= (5, 1):
            from django.db.migrations.autodetector import OperationDependency
            dependencies = [
                OperationDependency(
                    app_label,
                    model_name,
                    field_name,
                    OperationDependency.Type.REMOVE_ORDER_WRT,
                ),
                OperationDependency(
                    app_label,
                    model_name,
                    field_name,
                    OperationDependency.Type.ALTER_FOO_TOGETHER,
                ),
            ]
        else:
            dependencies = [
                (app_label, model_name, field_name, "order_wrt_unset"),
                (app_label, model_name, field_name, "foo_together_change"),
            ]

        self.add_operation(
            app_label,
            RemoveFieldComment(
                model_name=model_name,
                name=field_name,
            ),
            # We might need to depend on the removal of an
            # order_with_respect_to or index/unique_together operation;
            # this is safely ignored if there isn't one

            dependencies=dependencies,
        )

    def generate_altered_fields(self):
        """
        Make AlterField operations, or possibly RemovedField/AddField if alter
        isn't possible.
        """
        for app_label, model_name, field_name in sorted(
            self.old_field_keys & self.new_field_keys
        ):
            # Did the field change?
            old_model_name = self.renamed_models.get(
                (app_label, model_name), model_name
            )
            old_field_name = self.renamed_fields.get(
                (app_label, model_name, field_name), field_name
            )
            old_field = self.from_state.models[app_label, old_model_name].get_field(
                old_field_name
            )
            new_field = self.to_state.models[app_label, model_name].get_field(
                field_name
            )
            dependencies = []
            old_field_dec = self.deep_deconstruct(old_field)
            new_field_dec = self.deep_deconstruct(new_field)
            # If the field was confirmed to be renamed it means that only
            # db_column was allowed to change which generate_renamed_fields()
            # already accounts for by adding an AlterField operation.
            if old_field_dec != new_field_dec and old_field_name == field_name:
                both_m2m = old_field.many_to_many and new_field.many_to_many
                neither_m2m = not old_field.many_to_many and not new_field.many_to_many
                if both_m2m or neither_m2m:
                    # Either both fields are m2m or neither is
                    preserve_default = True
                    if (
                        old_field.null
                        and not new_field.null
                        and not new_field.has_default()
                        and new_field.db_default is models.NOT_PROVIDED
                        and not new_field.many_to_many
                    ):
                        field = new_field.clone()
                        new_default = self.questioner.ask_not_null_alteration(
                            field_name, model_name
                        )
                        if new_default is not models.NOT_PROVIDED:
                            field.default = new_default
                            preserve_default = False
                    else:
                        field = new_field
                    self.add_operation(
                        app_label,
                        AlterFieldComment(
                            model_name=model_name,
                            name=field_name,
                            field=field,
                            preserve_default=preserve_default,
                        ),
                        dependencies=dependencies,
                    )
                else:
                    # We cannot alter between m2m and concrete fields
                    self._generate_removed_field(app_label, model_name, field_name)
                    self._generate_added_field(app_label, model_name, field_name)

    def detect_index_changes(self):
        pass

    def drop_indexes(self):
        pass

    def generate_indexes(self):
        pass
