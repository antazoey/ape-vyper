import os
import re
from collections.abc import Iterable
from pathlib import Path
from site import getsitepackages
from typing import TYPE_CHECKING, Any, Optional

from ape.utils import ManagerAccessMixin, get_relative_path
from ethpm_types import ASTNode, ContractType, SourceMap
from ethpm_types.ast import ASTClassification
from ethpm_types.source import Content
from vvm import compile_standard as vvm_compile_standard  # type: ignore
from vvm.exceptions import VyperError  # type: ignore

from ape_vyper._utils import (
    DEV_MSG_PATTERN,
    EVM_VERSION_DEFAULT,
    FUNCTION_AST_TYPES,
    Optimization,
    get_evm_version_pragma_map,
    get_optimization_pragma_map,
    get_pcmap,
)
from ape_vyper.compiler._versions.utils import output_details
from ape_vyper.exceptions import VyperCompileError

if TYPE_CHECKING:
    from ape.managers.project import ProjectManager
    from packaging.version import Version

    from ape_vyper.compiler.api import VyperCompiler
    from ape_vyper.config import VyperConfig
    from ape_vyper.imports import ImportMap


class BaseVyperCompiler(ManagerAccessMixin):
    """
    Shared logic between all versions of Vyper.
    """

    def __init__(self, api: "VyperCompiler"):
        self.api = api

    @property
    def config(self) -> "VyperConfig":
        return self.config_manager.vyper  # type: ignore

    def get_output_format(self, project: Optional["ProjectManager"] = None) -> list[str]:
        pm = project or self.local_project
        return pm.config.vyper.output_format or ["*"]

    def get_evm_version(self, version: "Version") -> Optional[str]:
        return self.config.evm_version or EVM_VERSION_DEFAULT.get(version.base_version)

    def get_import_remapping(self, project: Optional["ProjectManager"] = None) -> dict[str, dict]:
        # Overridden on 0.4 to not use.
        # Import-remapping is for Vyper versions 0.2 - 0.3 to create the interface dict.
        pm = project or self.local_project
        dependencies = self.api.get_dependencies(project=pm, allow_compile=True)

        interfaces: dict[str, dict] = {}
        for key, dependency_project in dependencies.items():
            manifest = dependency_project.manifest

            for name, ct in (manifest.contract_types or {}).items():
                filename = f"{key}/{name}.json"
                abi_list = [x.model_dump(mode="json", by_alias=True) for x in ct.abi]
                interfaces[filename] = {"abi": abi_list}

        return interfaces

    def compile(
        self,
        vyper_version: "Version",
        settings: dict,
        import_map: "ImportMap",
        project: Optional["ProjectManager"] = None,
    ):
        pm = project or self.local_project
        for settings_key, settings_set in settings.items():
            if not (output_selection := settings_set.get("outputSelection", {})):
                continue

            src_dict = self._get_sources_dictionary(
                output_selection,
                project=pm,
                import_map=import_map,
            )

            input_json: dict = {
                "language": "Vyper",
                "settings": settings_set,
                "sources": src_dict,
            }

            if interfaces := self.get_import_remapping(project=project):
                input_json["interfaces"] = interfaces

            # Output compiler details.
            output_details(*output_selection.keys(), version=vyper_version)

            try:
                result = vvm_compile_standard(
                    input_json, vyper_version=vyper_version, base_path=pm.path
                )
            except VyperError as err:
                raise VyperCompileError(err) from err

            for source_id, output_items in result["contracts"].items():
                if source_id not in src_dict:
                    # Handle oddity from Vyper 0.3.0 where absolute paths may have
                    # weird prefix.
                    if f"{os.path.sep}{source_id}" in src_dict:
                        source_id = f"{os.path.sep}{source_id}"
                    else:
                        continue

                content = Content.model_validate(src_dict[source_id].get("content", ""))
                for name, output in output_items.items():
                    # De-compress source map to get PC POS map.
                    if "ast" in result["sources"][source_id]:
                        ast = self._parse_ast(result["sources"][source_id]["ast"], content)
                    else:
                        ast = None

                    evm = output.get("evm", {})
                    runtime_bytecode = evm.get("deployedBytecode", {})
                    opcodes = runtime_bytecode.get("opcodes", "").split(" ")

                    if "sourceMap" in runtime_bytecode:
                        compressed_src_map = SourceMap(root=runtime_bytecode["sourceMap"])
                        src_map = list(compressed_src_map.parse())[1:]
                        pcmap = self._get_pcmap(
                            vyper_version, ast, src_map, opcodes, runtime_bytecode
                        )
                    else:
                        compressed_src_map = None
                        src_map = None
                        pcmap = None

                    # Find content-specified dev messages.
                    dev_messages = {}
                    for line_no, line in content.root.items():
                        if match := re.search(DEV_MSG_PATTERN, line):
                            dev_messages[line_no] = match.group(1).strip()

                    source_id_path = Path(source_id)
                    if source_id_path.is_absolute():
                        final_source_id = f"{get_relative_path(Path(source_id), pm.path)}"
                    else:
                        final_source_id = source_id

                    deployment_bytecode = evm.get("bytecode", {}).get("object")
                    contract_type = ContractType.model_validate(
                        {
                            "ast": ast,
                            "contractName": name,
                            "sourceId": final_source_id,
                            "deploymentBytecode": (
                                {"bytecode": deployment_bytecode} if deployment_bytecode else {}
                            ),
                            "runtimeBytecode": (
                                {"bytecode": runtime_bytecode["object"]} if runtime_bytecode else {}
                            ),
                            "abi": output.get("abi"),
                            "sourcemap": compressed_src_map,
                            "pcmap": pcmap,
                            "userdoc": output.get("userdoc"),
                            "devdoc": output.get("devdoc"),
                            "dev_messages": dev_messages,
                        }
                    )
                    yield contract_type, settings_key

    def _parse_ast(self, ast: dict, content: Content) -> ASTNode:
        ast_model = ASTNode.model_validate(ast)
        self._classify_ast(ast_model)

        # Track function offsets.
        function_offsets = []
        for node in ast_model.children:
            lineno = node.lineno

            # NOTE: Constructor is handled elsewhere.
            if node.ast_type == "FunctionDef" and "__init__" not in content.root.get(lineno, ""):
                function_offsets.append((node.lineno, node.end_lineno))

        return ast_model

    def get_settings(
        self,
        version: "Version",
        source_paths: Iterable[Path],
        project: Optional["ProjectManager"] = None,
    ) -> dict:
        pm = project or self.local_project
        default_optimization = self._get_default_optimization(version)
        output_selection: dict[str, set[str]] = {}
        optimizations_map = get_optimization_pragma_map(source_paths, pm.path, default_optimization)
        evm_version_map = get_evm_version_pragma_map(source_paths, pm.path)
        default_evm_version = self.get_evm_version(version)
        for source_path in source_paths:
            source_id = str(get_relative_path(source_path.absolute(), pm.path))

            if not (optimization := optimizations_map.get(source_id)):
                optimization = self._get_default_optimization(version)

            evm_version = evm_version_map.get(source_id, default_evm_version)
            settings_key = f"{optimization}%{evm_version}".lower()
            if settings_key not in output_selection:
                output_selection[settings_key] = {source_id}
            else:
                output_selection[settings_key].add(source_id)

        version_settings: dict[str, dict] = {}
        for settings_key, selection in output_selection.items():
            optimization, evm_version = settings_key.split("%")
            if optimization == "true":
                optimization = True
            elif optimization == "false":
                optimization = False

            selection_dict = self._get_selection_dictionary(selection, project=pm)
            search_paths = [*getsitepackages(), "."]

            version_settings[settings_key] = {
                "optimize": optimization,
                "outputSelection": selection_dict,
                "search_paths": search_paths,
            }
            if evm_version and evm_version not in ("none", "null"):
                version_settings[settings_key]["evmVersion"] = f"{evm_version}"

        return version_settings

    def _classify_ast(self, _node: ASTNode):
        if _node.ast_type in FUNCTION_AST_TYPES:
            _node.classification = ASTClassification.FUNCTION

        for child in _node.children:
            self._classify_ast(child)

    def _get_sources_dictionary(
        self, source_ids: Iterable[str], project: Optional["ProjectManager"] = None, **kwargs
    ) -> dict[str, dict]:
        """
        Generate input for the "sources" key in the input JSON.
        """
        pm = project or self.local_project
        return {
            s: {"content": p.read_text(encoding="utf8")}
            for s, p in {src_id: pm.path / src_id for src_id in source_ids}.items()
            if p.parent != pm.path / "interfaces"
        }

    def _get_selection_dictionary(
        self,
        selection: Iterable[str],
        project: Optional["ProjectManager"] = None,
        **kwargs,
    ) -> dict:
        """
        Generate input for the "outputSelection" key in the input JSON.
        """
        # NOTE: Vyper0.2 and Vyper0.3 versions don't override this.
        #   Interfaces cannot be in the sources dict for those versions
        #   (whereas in Vyper0.4, they must).
        pm = project or self.local_project
        return {
            s: self.get_output_format(project=pm)
            for s in selection
            if (pm.path / s).is_file()
            if "interfaces" not in s
        }

    def _get_pcmap(
        self,
        vyper_version: "Version",
        ast: Any,
        src_map: list,
        opcodes: list[str],
        bytecode: dict,
    ):
        """
        Generate the PCMap.
        """
        return get_pcmap(bytecode)

    def _get_default_optimization(self, vyper_version: "Version") -> Optimization:
        """
        The default  value for "optimize" in the settings for input JSON.
        """
        return True
