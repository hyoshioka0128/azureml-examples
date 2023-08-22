# imports
import contextlib
import os
import json
import glob
import argparse
import hashlib

from configparser import ConfigParser

# define constants
ENABLE_MANUAL_CALLING = True  # defines whether the workflow can be invoked or not
NOT_TESTED_NOTEBOOKS = [
    "datastore",
    "mlflow-model-local-inference-test",
    "multicloud-configuration",
    "debug-online-endpoints-locally-in-visual-studio-code",
    "train-hyperparameter-tune-with-sklearn",
    "train-hyperparameter-tune-deploy-with-keras",
    "train-hyperparameter-tune-deploy-with-tensorflow",
    "interactive_data_wrangling",
    # mlflow SDK samples notebooks
    "mlflow_sdk_online_endpoints_progresive",
    "mlflow_sdk_online_endpoints",
    "mlflow_sdk_web_service",
    "scoring_to_mlmodel",
    "track_with_databricks_deploy_aml",
    "model_management",
    "run_history",
    "keras_mnist_with_mlflow",
    "logging_and_customizing_models",
    "xgboost_classification_mlflow",
    "xgboost_nested_runs",
    "xgboost_service_principal",
    "using_mlflow_rest_api",
    "yolov5/tutorial",
]  # cannot automate lets exclude
NOT_SCHEDULED_NOTEBOOKS = []  # these are too expensive, lets not run everyday
# define branch where we need this
# use if running on a release candidate, else make it empty
READONLY_HEADER = "# This code is autogenerated.\
\n# Code is generated by running custom script: python3 readme.py\
\n# Any manual changes to this file may cause incorrect behavior.\
\n# Any manual changes will be overwritten if the code is regenerated.\n"
BRANCH = "main"  # default - do not change
# BRANCH = "sdk-preview"  # this should be deleted when this branch is merged to main
GITHUB_CONCURRENCY_GROUP = (
    "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
)
USE_FORECAST_REQUIREMENTS = "USE_FORECAST_REQUIREMENTS"
COMPUTE_NAMES = "COMPUTE_NAMES"


def main(args):
    # get list of notebooks
    notebooks = sorted(glob.glob("**/*.ipynb", recursive=True))

    # write workflows
    write_workflows(notebooks)

    # modify notebooks
    modify_notebooks(notebooks)

    # write readme
    write_readme(notebooks)

    # write pipeline readme
    pipeline_dir = "jobs" + os.sep + "pipelines" + os.sep
    with change_working_dir(pipeline_dir):
        pipeline_notebooks = sorted(glob.glob("**/*.ipynb", recursive=True))
    pipeline_notebooks = [
        f"{pipeline_dir}{notebook}" for notebook in pipeline_notebooks
    ]
    write_readme(pipeline_notebooks, pipeline_folder=pipeline_dir)


def write_workflows(notebooks):
    print("writing .github/workflows...")
    cfg = ConfigParser()
    cfg.read(os.path.join("notebooks_config.ini"))
    for notebook in notebooks:
        notebook_path = notebook.replace(os.sep, "/")
        if not any(excluded in notebook_path for excluded in NOT_TESTED_NOTEBOOKS):
            # get notebook name
            name = os.path.basename(notebook).replace(".ipynb", "")
            folder = os.path.dirname(notebook)
            classification = folder.replace(os.sep, "-")

            enable_scheduled_runs = True
            if any(excluded in notebook_path for excluded in NOT_SCHEDULED_NOTEBOOKS):
                enable_scheduled_runs = False

            # write workflow file
            write_notebook_workflow(
                notebook, name, classification, folder, enable_scheduled_runs, cfg
            )
    print("finished writing .github/workflows")


def get_additional_requirements(req_name, req_path):
    return f"""
    - name: pip install {req_name} reqs
      run: pip install -r {req_path}"""


def get_mlflow_import(notebook, validation_yml):
    with open(notebook, "r", encoding="utf-8") as f:
        string_file = f.read()
        if (
            validation_yml
            or "import mlflow" in string_file
            or "from mlflow" in string_file
        ):
            return get_additional_requirements(
                "mlflow", "sdk/python/mlflow-requirements.txt"
            )
        else:
            return ""


def get_forecast_reqs(notebook_name, nb_config):
    is_required = int(
        nb_config.get(
            section=notebook_name, option=USE_FORECAST_REQUIREMENTS, fallback=0
        )
    )
    if is_required:
        return get_additional_requirements(
            "forecasting", "sdk/python/forecasting-requirements.txt"
        )
    else:
        return ""


def get_validation_yml(notebook_folder, notebook_name):
    validation_yml = ""
    validation_json_file_name = os.path.join(
        "..",
        "..",
        ".github",
        "test",
        "sdk",
        notebook_name.replace(".ipynb", ".json"),
    )

    if os.path.exists(validation_json_file_name):
        with open(validation_json_file_name, "r") as json_file:
            validation_file = json.load(json_file)
            for validation in validation_file["validations"]:
                validation_yml += get_validation_check_yml(
                    notebook_folder, notebook_name, validation
                )

    return validation_yml


def get_validation_check_yml(notebook_folder, notebook_name, validation):
    validation_name = validation["name"]
    validation_file_name = validation_name.replace(" ", "_")
    notebook_output_file = (
        os.path.basename(notebook_name).replace(".", ".output.").replace(os.sep, "/")
    )
    notebook_folder = notebook_folder.replace(os.sep, "/")
    full_folder_name = f"sdk/python/{notebook_folder}"
    github_workspace = "${{ github.workspace }}"

    check_yml = f"""
    - name: {validation_name}
      run: |
         python {github_workspace}/.github/test/scripts/{validation_file_name}.py \\
                --file_name {notebook_output_file} \\
                --folder . \\"""

    for param_name, param_value in validation["params"].items():
        if type(param_value) is list:
            check_yml += f"""
                --{param_name} \\"""

            for param_item in param_value:
                param_item_value = param_item.replace("\n", "\\n")
                check_yml += f"""
                  \"{param_item_value}\" \\"""
        else:
            check_yml += f"""
                --{param_name} {param_value} \\"""

    check_yml += f"""
      working-directory: {full_folder_name} \\"""

    return check_yml[:-2]


def write_notebook_workflow(
    notebook, name, classification, folder, enable_scheduled_runs, nb_config
):
    is_pipeline_notebook = ("jobs-pipelines" in classification) or (
        "assets-component" in classification
    )
    is_spark_notebook_sample = ("jobs-spark" in classification) or ("_spark_" in name)
    is_featurestore_sample = "featurestore_sample" in classification
    creds = "${{secrets.AZUREML_CREDENTIALS}}"
    # Duplicate name in working directory during checkout
    # https://github.com/actions/checkout/issues/739
    github_workspace = "${{ github.workspace }}"
    forecast_import = get_forecast_reqs(name, nb_config)
    posix_folder = folder.replace(os.sep, "/")
    posix_notebook = notebook.replace(os.sep, "/")

    # Schedule notebooks at different times to reduce maximum quota usage.
    name_hash = int(hashlib.sha512(name.encode()).hexdigest(), 16)
    schedule_minute = name_hash % 60
    hours_between_runs = 12
    schedule_hour = (name_hash // 60) % hours_between_runs

    validation_yml = get_validation_yml(folder, notebook)
    mlflow_import = get_mlflow_import(notebook, validation_yml)

    workflow_yaml = f"""{READONLY_HEADER}
name: sdk-{classification}-{name}
# This file is created by sdk/python/readme.py.
# Please do not edit directly.
on:\n"""
    if ENABLE_MANUAL_CALLING:
        workflow_yaml += f"""  workflow_dispatch:\n"""
    if enable_scheduled_runs:
        workflow_yaml += f"""  schedule:
    - cron: "{schedule_minute} {schedule_hour}/{hours_between_runs} * * *"\n"""
    workflow_yaml += f"""  pull_request:
    branches:
      - main\n"""
    if BRANCH != "main":
        workflow_yaml += f"""      - {BRANCH}\n"""
        if is_pipeline_notebook:
            workflow_yaml += "      - pipeline/*\n"
    workflow_yaml += f"""    paths:
      - sdk/python/{posix_folder}/**
      - .github/workflows/sdk-{classification}-{name}.yml
      - sdk/python/dev-requirements.txt
      - infra/bootstrapping/**
      - sdk/python/setup.sh
concurrency:
  group: {GITHUB_CONCURRENCY_GROUP}
  cancel-in-progress: true
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
    - name: check out repo
      uses: actions/checkout@v2
    - name: setup python
      uses: actions/setup-python@v2
      with:
        python-version: "3.8"
    - name: pip install notebook reqs
      run: pip install -r sdk/python/dev-requirements.txt{mlflow_import}{forecast_import}
    - name: azure login
      uses: azure/login@v1
      with:
        creds: {creds}
    - name: bootstrap resources
      run: |
          echo '{GITHUB_CONCURRENCY_GROUP}';
          bash bootstrap.sh
      working-directory: infra/bootstrapping
      continue-on-error: false
    - name: setup SDK
      run: |
          source "{github_workspace}/infra/bootstrapping/sdk_helpers.sh";
          source "{github_workspace}/infra/bootstrapping/init_environment.sh";
          bash setup.sh
      working-directory: sdk/python
      continue-on-error: true
    - name: setup-cli
      run: |
          source "{github_workspace}/infra/bootstrapping/sdk_helpers.sh";
          source "{github_workspace}/infra/bootstrapping/init_environment.sh";
          bash setup.sh
      working-directory: cli
      continue-on-error: true\n"""
    if is_spark_notebook_sample:
        workflow_yaml += get_spark_config_workflow(posix_folder, name)
    if is_featurestore_sample:
        workflow_yaml += get_featurestore_config_workflow(posix_folder, name)
    workflow_yaml += f"""    - name: run {posix_notebook}
      run: |
          source "{github_workspace}/infra/bootstrapping/sdk_helpers.sh";
          source "{github_workspace}/infra/bootstrapping/init_environment.sh";
          bash "{github_workspace}/infra/bootstrapping/sdk_helpers.sh" generate_workspace_config "../../.azureml/config.json";
          bash "{github_workspace}/infra/bootstrapping/sdk_helpers.sh" replace_template_values "{name}.ipynb";
          [ -f "../../.azureml/config" ] && cat "../../.azureml/config";"""

    if name == "debug-online-endpoints-locally-in-visual-studio-code":
        workflow_yaml += f"""
          sed -i -e "s/<ENDPOINT_NAME>/localendpoint/g" {name}.ipynb

          # Create a dummy executable for VSCode
          mkdir -p /tmp/code
          touch /tmp/code/code
          chmod +x /tmp/code/code
          export PATH="/tmp/code:$PATH"\n"""

    if not ("automl" in folder):
        workflow_yaml += f"""
          papermill -k python {name}.ipynb {name}.output.ipynb
      working-directory: sdk/python/{posix_folder}"""
    elif "nlp" in folder or "image" in folder:
        # need GPU cluster, so override the compute cluster name to dedicated
        workflow_yaml += f"""
          papermill -k python -p compute_name automl-gpu-cluster {name}.ipynb {name}.output.ipynb
      working-directory: sdk/python/{posix_folder}"""
    else:
        # need CPU cluster, so override the compute cluster name to dedicated
        workflow_yaml += f"""
          papermill -k python -p compute_name automl-cpu-cluster {name}.ipynb {name}.output.ipynb
      working-directory: sdk/python/{posix_folder}"""

    if name == "connections":
        workflow_yaml += """
      env:
        ACR_USERNAME: ${{ secrets.ACR_USERNAME }}
        ACR_PASSWORD: ${{ secrets.ACR_PASSWORD }}
        GIT_PAT: ${{ secrets.GIT_PAT }}
        PYTHON_FEED_SAS: ${{ secrets.PYTHON_FEED_SAS }}"""

    workflow_yaml += validation_yml

    workflow_yaml += f"""
    - name: upload notebook's working folder as an artifact
      if: ${{{{ always() }}}}
      uses: actions/upload-artifact@v2
      with:
        name: {name}
        path: sdk/python/{posix_folder}\n"""

    if nb_config.get(section=name, option=COMPUTE_NAMES, fallback=None):
        workflow_yaml += f"""
    - name: Remove the compute if notebook did not done it properly.
      run: bash "{github_workspace}/infra/bootstrapping/remove_computes.sh" {nb_config.get(section=name, option=COMPUTE_NAMES)}\n"""

    workflow_file = os.path.join(
        "..", "..", ".github", "workflows", f"sdk-{classification}-{name}.yml"
    )
    workflow_before = ""
    if os.path.exists(workflow_file):
        with open(workflow_file, "r") as f:
            workflow_before = f.read()

    if workflow_yaml != workflow_before:
        # write workflow
        with open(workflow_file, "w") as f:
            f.write(workflow_yaml)


def write_readme(notebooks, pipeline_folder=None):
    prefix = "prefix.md"
    suffix = "suffix.md"
    readme_file = "README.md"
    if pipeline_folder:
        prefix = os.path.join(pipeline_folder, prefix)
        suffix = os.path.join(pipeline_folder, suffix)
        readme_file = os.path.join(pipeline_folder, readme_file)

    if BRANCH == "":
        branch = "main"
    else:
        branch = BRANCH
        # read in prefix.md and suffix.md
        with open(prefix, "r") as f:
            prefix = f.read()
        with open(suffix, "r") as f:
            suffix = f.read()

        # define markdown tables
        notebook_table = f"Test Status is for branch - **_{branch}_**\n|Area|Sub-Area|Notebook|Description|Status|\n|--|--|--|--|--|\n"
        for notebook in notebooks:
            # get notebook name
            name = notebook.split(os.sep)[-1].replace(".ipynb", "")
            area = notebook.split(os.sep)[0]
            sub_area = notebook.split(os.sep)[1]
            folder = os.path.dirname(notebook)
            classification = folder.replace(os.sep, "-")

            try:
                # read in notebook
                with open(notebook, "r", encoding="utf-8") as f:
                    data = json.load(f)

                description = "*no description*"
                try:
                    if data["metadata"]["description"] is not None:
                        description = data["metadata"]["description"]["description"]
                except BaseException:
                    pass
            except BaseException:
                print("Could not load", notebook)
                pass

            if any(excluded in notebook for excluded in NOT_TESTED_NOTEBOOKS):
                description += " - _This sample is excluded from automated tests_"
            if any(excluded in notebook for excluded in NOT_SCHEDULED_NOTEBOOKS):
                description += " - _This sample is only tested on demand_"

            if pipeline_folder:
                notebook = os.path.relpath(notebook, pipeline_folder)

            # write workflow file
            notebook_table += (
                write_readme_row(
                    branch,
                    notebook.replace(os.sep, "/"),
                    name,
                    classification,
                    area,
                    sub_area,
                    description,
                )
                + "\n"
            )

        print("writing README.md...")
        with open(readme_file, "w") as f:
            f.write(prefix + notebook_table + suffix)
        print("finished writing README.md")


def write_readme_row(
    branch, notebook, name, classification, area, sub_area, description
):
    gh_link = "https://github.com/Azure/azureml-examples/actions/workflows"

    nb_name = f"[{name}]({notebook})"
    status = f"[![{name}]({gh_link}/sdk-{classification}-{name}.yml/badge.svg?branch={branch})]({gh_link}/sdk-{classification}-{name}.yml)"

    row = f"|{area}|{sub_area}|{nb_name}|{description}|{status}|"
    return row


def modify_notebooks(notebooks):
    print("modifying notebooks...")
    # setup variables
    kernelspec = {
        "display_name": "Python 3.10 - SDK V2",
        "language": "python",
        "name": "python310-sdkv2",
    }

    # for each notebooks
    for notebook in notebooks:
        # read in notebook
        with open(notebook, "r", encoding="utf-8") as f:
            data = json.load(f)

        # update metadata
        data["metadata"]["kernelspec"] = kernelspec

        # write notebook
        with open(notebook, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
            f.write("\n")

    print("finished modifying notebooks...")


def get_spark_config_workflow(folder_name, file_name):
    workflow = f"""    - name: setup spark resources
      run: |
          bash -x jobs/spark/setup_spark.sh jobs/spark/ {folder_name}/{file_name}.ipynb
      working-directory: sdk/python
      continue-on-error: true\n"""

    return workflow


def get_featurestore_config_workflow(folder_name, file_name):
    workflow = f"""    - name: setup feature-store resources
      run: |
          bash -x setup-resources.sh automation/{file_name}.ipynb
      working-directory: sdk/python/featurestore_sample
      continue-on-error: true\n"""

    return workflow


@contextlib.contextmanager
def change_working_dir(path):
    """Context manager for changing the current working directory"""

    saved_path = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(saved_path)


# run functions
if __name__ == "__main__":
    # setup argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-readme", type=bool, default=False)
    args = parser.parse_args()

    # call main
    main(args)
