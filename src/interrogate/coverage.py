# Copyright 2020 Lynn Root
"""Measure and report on documentation coverage in Python modules."""
import ast
import os
import sys

from pathlib import Path

import attr
import click
import tabulate

from py import io as py_io

from interrogate import config
from interrogate import utils
from interrogate import visit


tabulate.PRESERVE_WHITESPACE = True


@attr.s
class BaseInterrogateResult:
    """Base results class.

    :attr int total: total number of objects interrogated (modules,
        classes, methods, and functions).
    :attr int covered: number of objects covered by docstrings.
    :attr int missing: number of objects not covered by docstrings.
    """

    total = attr.ib(init=False, default=0)
    covered = attr.ib(init=False, default=0)
    missing = attr.ib(init=False, default=0)

    @property
    def perc_covered(self):
        """Percentage of node covered.

        :return: percentage covered over total.
        :rtype: float
        """
        # technically this shouldn't happen since empty files still have
        # a total of one (module-level)
        if self.total == 0:  # pragma: no cover
            return 0
        return (float(self.covered) / float(self.total)) * 100


@attr.s
class InterrogateFileResult(BaseInterrogateResult):
    """Coverage results for a particular file.

    :param str filename: filename associated with the coverage result.
    :param bool ignore_module: whether or not to ignore this file/module.
    :param visit.CoverageVisitor visitor: coverage visitor instance
        that assessed docstring coverage of file.
    """

    filename = attr.ib(default=None)
    ignore_module = attr.ib(default=False)
    nodes = attr.ib(repr=False, default=None)

    def combine(self):
        """Tally results from each AST node visited."""
        for node in self.nodes:
            if node.node_type == "Module":
                if self.ignore_module:
                    continue
            self.total += 1
            if node.covered:
                self.covered += 1

        self.missing = self.total - self.covered


@attr.s
class InterrogateResults(BaseInterrogateResult):
    """Coverage results for all files.

    :attr int ret_code: return code of program (``0`` for success, ``1``
        for fail).
    :attr list(InterrogateFileResults) file_results: list of file
        results associated with this program run.
    """

    ret_code = attr.ib(init=False, default=0, repr=False)
    file_results = attr.ib(init=False, default=None, repr=False)

    def combine(self):
        """Tally results from each file."""
        for result in self.file_results:
            self.covered += result.covered
            self.missing += result.missing
            self.total += result.total


class InterrogateCoverage:
    """The doc coverage interrogator!

    :param list(str) paths: list of paths to interrogate.
    :param config.InterrogateConfig conf: interrogation configuration.
    :param tuple(str) excluded: tuple of files and directories to exclude
        in assessing coverage.
    """

    COMMON_EXCLUDE = [".tox", ".venv", "venv", ".git", ".hg"]

    def __init__(self, paths, conf=None, excluded=None):
        self.paths = paths
        self.config = conf or config.InterrogateConfig()
        self.excluded = excluded or ()
        self.common_base = Path("/")
        self._add_common_exclude()

    def _add_common_exclude(self):
        """Ignore common directories by default"""
        for path in self.paths:
            self.excluded = self.excluded + tuple(
                path / i for i in self.COMMON_EXCLUDE
            )

    def _filter_files(self, files):
        """Filter files that are explicitly excluded."""
        for f in files:
            if f.suffix != ".py":
                continue
            if self.config.ignore_init_module:
                basename = f.name
                if basename == "__init__.py":
                    continue
            maybe_excluded_dir = any(
                [str(f).startswith(str(exc)) for exc in self.excluded]
            )
            maybe_excluded_file = f in self.excluded
            if any([maybe_excluded_dir, maybe_excluded_file]):
                continue
            yield f

    def get_filenames_from_paths(self):
        """Find all files to measure for docstring coverage."""
        filenames = []
        for path in self.paths:
            if path.is_file():
                if path.suffix != ".py":
                    msg = (
                        "E: Invalid file '{}'. Unable interrogate non-Python "
                        "files.".format(path)
                    )
                    click.echo(msg, err=True)
                    return sys.exit(1)
                filenames.append(path)
                continue
            for root, dirs, fs in os.walk(path):
                root = Path(root)
                full_paths = [root / f for f in fs]
                filenames.extend(self._filter_files(full_paths))

        if not filenames:
            p = ", ".join([str(path) for path in self.paths])
            msg = "E: No Python files found to interrogate in '{}'.".format(p)
            click.echo(msg, err=True)
            return sys.exit(1)

        self.common_base = utils.get_common_base(filenames)
        return filenames

    def _filter_nodes(self, nodes):
        """Remove empty modules when ignoring modules."""
        is_empty = 1 == len(nodes)
        if is_empty and self.config.ignore_module:
            return []

        if not self.config.include_regex:
            return nodes

        # FIXME: any methods, closures, inner functions/classes, etc
        #        will print out with extra indentation if the parent
        #        does not meet the whitelist
        filtered = []
        module_node = None
        for node in nodes:
            if node.node_type == "Module":
                module_node = node
                continue
            for regexp in self.config.include_regex:
                match = regexp.match(node.name)
                if match:
                    filtered.append(node)
        if module_node and filtered:
            filtered.insert(0, module_node)
        return filtered

    def _get_file_coverage(self, filename):
        """Get coverage results for a particular file."""
        with open(filename, "r", encoding="utf-8") as f:
            source_tree = f.read()

        parsed_tree = ast.parse(source_tree)
        visitor = visit.CoverageVisitor(filename=filename, config=self.config)
        visitor.visit(parsed_tree)

        filtered_nodes = self._filter_nodes(visitor.nodes)
        if len(filtered_nodes) == 0:
            return

        results = InterrogateFileResult(
            filename=filename,
            ignore_module=self.config.ignore_module,
            nodes=filtered_nodes,
        )
        results.combine()
        return results

    def _get_coverage(self, filenames):
        """Get coverage results."""
        results = InterrogateResults()
        file_results = []
        for f in filenames:
            result = self._get_file_coverage(f)
            if result:
                file_results.append(result)
        results.file_results = file_results

        results.combine()

        if self.config.fail_under > results.perc_covered:
            results.ret_code = 1

        return results

    def get_coverage(self):
        """Get coverage results from files."""
        filenames = self.get_filenames_from_paths()
        return self._get_coverage(filenames)

    def _get_filename(self, filename):
        """Get filename for output information.

        If only one file is being interrogated, then ``self.common_base``
        and ``filename`` will be the same. Therefore, take the file
        ``Path.name`` as the return ``filename``.
        """
        if filename == self.common_base:
            return filename.name
        return filename.relative_to(self.common_base)

    def _get_detailed_row(self, node, filename):
        """Generate a row of data for the detailed view."""
        filename = self._get_filename(filename)

        if node.node_type == "Module":
            if self.config.ignore_module:
                return [filename, ""]
            name = "{} (module)".format(filename)
        else:
            name = node.path.split(":")[-1]
            name = "{} (L{})".format(name, node.lineno)

        padding = "  " * node.level
        name = "{}{}".format(padding, name)
        status = "MISSED" if not node.covered else "COVERED"
        return [name, status]

    def _create_detailed_table(self, combined_results):
        """Generate table for the detailed view.

        The detailed view shows coverage of each module, class, and
        function/method.
        """

        def _sort_nodes(x):
            """Sort nodes by line number."""
            lineno = getattr(x, "lineno", 0)
            # lineno is "None" if module is empty
            if lineno is None:
                lineno = 0
            return lineno

        verbose_tbl = []
        header = ["Name", "Status"]
        verbose_tbl.append(header)
        verbose_tbl.append(utils.TABLE_SEPARATOR)
        for file_result in combined_results.file_results:
            nodes = file_result.nodes
            nodes = sorted(nodes, key=_sort_nodes)
            for n in nodes:
                verbose_tbl.append(
                    self._get_detailed_row(n, file_result.filename)
                )
            verbose_tbl.append(utils.TABLE_SEPARATOR)
        return verbose_tbl

    def _print_detailed_table(self, results, tw):
        """Print detailed table to the given output stream."""
        detailed_table = self._create_detailed_table(results)
        to_print = tabulate.tabulate(
            detailed_table,
            tablefmt=utils.InterrogateTableFormat,
            colalign=["left", "right"],
        )
        tw.sep("-", "Detailed Coverage", fullwidth=utils.TERMINAL_WIDTH)
        tw.line(to_print)
        tw.line()

    def _create_summary_table(self, combined_results):
        """Generate table for the summary view.

        The summary view shows coverage for an overall file.
        """
        table = []
        header = ["Name", "Total", "Miss", "Cover", "Cover%"]
        table.append(header)
        table.append(utils.TABLE_SEPARATOR)

        for file_result in combined_results.file_results:
            filename = self._get_filename(file_result.filename)
            perc_covered = "{:.0f}%".format(file_result.perc_covered)
            row = [
                str(filename),
                file_result.total,
                file_result.missing,
                file_result.covered,
                perc_covered,
            ]
            table.append(row)

        table.append(utils.TABLE_SEPARATOR)
        total_perc_covered = "{:.1f}%".format(combined_results.perc_covered)
        total_row = [
            "TOTAL",
            combined_results.total,
            combined_results.missing,
            combined_results.covered,
            total_perc_covered,
        ]
        table.append(total_row)
        return table

    def _print_summary_table(self, results, tw):
        """Print summary table to the given output stream."""
        summary_table = self._create_summary_table(results)
        tw.sep("-", title="Summary", fullwidth=utils.TERMINAL_WIDTH)
        to_print = tabulate.tabulate(
            summary_table,
            tablefmt=utils.InterrogateTableFormat,
            colalign=("left", "right", "right", "right", "right"),
        )
        tw.line(to_print)

    @staticmethod
    def _sort_results(results):
        """Sort results by filename, directories first"""
        all_filenames_map = {r.filename: r for r in results.file_results}
        all_dirs = sorted(
            set(
                [
                    r.filename.parent
                    for r in results.file_results
                    if r.filename.parent != ""
                ]
            )
        )

        sorted_results = []
        while all_dirs:
            current_dir = all_dirs.pop()
            files = []
            for p in os.listdir(current_dir):
                path = current_dir / p
                if path in all_filenames_map.keys():
                    files.append(path)
            files = sorted(files)
            sorted_results.extend(files)

        sorted_res = []
        for filename in sorted_results:
            sorted_res.append(all_filenames_map[filename])
        results.file_results = sorted_res
        return results

    def print_results(self, results, output, verbosity):
        """Print results to a given output stream.

        :param InterrogateResults results: results of docstring coverage
            interrogation.
        :param output: filename to output results. If ``None``, uses
            ``sys.stdout``.
        :type output: ``str`` or ``None``
        :param int verbosity: level of detail to print out (``0``-``2``).
        """
        with utils.smart_open(output, "w") as f:
            tw = py_io.TerminalWriter(file=f)
            results = self._sort_results(results)
            if verbosity > 0:
                base = self.common_base
                if base.is_file():
                    base = base.parent
                tw.sep(
                    "=",
                    "Coverage for {}/".format(base),
                    fullwidth=utils.TERMINAL_WIDTH,
                )
            if verbosity > 1:
                self._print_detailed_table(results, tw)
            if verbosity > 0:
                self._print_summary_table(results, tw)

            status = "PASSED"
            if results.ret_code > 0:
                status = "FAILED"

            status_line = "RESULT: {} (minimum: {}%, actual: {:.1f}%)".format(
                status, self.config.fail_under, results.perc_covered
            )
            if verbosity > 0:
                tw.sep("-", title=status_line, fullwidth=utils.TERMINAL_WIDTH)
            else:
                tw.line(status_line)
