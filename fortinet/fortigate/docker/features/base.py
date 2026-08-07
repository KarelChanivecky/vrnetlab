"""Shared feature lifecycle primitives."""


class Feature:
    def __init__(self, vm, commander, name):
        self.vm = vm
        self.commander = commander
        self.name = name
        self._completed = False

    @property
    def completed(self):
        return self._completed

    def mark_completed(self):
        self._completed = True

    def begin_activation(self):
        """Prepare this feature instance for an initial or later command stage."""
        self._completed = False

    def activate(self, commander):
        commander.feature_complete(self)

    def on_output(self, commander, attempt, output):
        pass

    def on_command_result(self, commander, attempt, state, output):
        pass

    def on_block_complete(self, commander):
        commander.feature_complete(self)

    def on_session_loss(self, commander, attempt):
        return attempt.spec.session_loss

    def reversal_blocks(self):
        """Return command blocks that completely reverse this feature's work."""
        return []

    def undo(self):
        """Return a feature stage that reverses this completed feature."""
        return _FeatureUndo(self.vm, self.commander, self)

    @property
    def file_path(self):
        """Return the optional file path watched for this feature."""
        return None

    def on_file_detected(self, path):
        pass

    def on_file_deleted(self, path):
        pass

    def on_file_modified(self, path):
        pass


class StaticFeature(Feature):
    """A feature whose command blocks are known when it is constructed."""

    def __init__(self, vm, commander, name, blocks, on_complete=None):
        super().__init__(vm, commander, name)
        self._blocks = list(blocks)
        self._on_complete = on_complete

    def activate(self, commander):
        self._submit_next(commander)

    def _submit_next(self, commander):
        if self._blocks:
            commander.submit_block(self, self._blocks.pop(0))
            return
        if self._on_complete:
            self._on_complete(commander)
        commander.feature_complete(self)

    def on_block_complete(self, commander):
        self._submit_next(commander)


class _FeatureUndo(StaticFeature):
    """A scheduled reversal stage created by ``Feature.undo``."""

    def __init__(self, vm, commander, target):
        super().__init__(vm, commander, f"undo-{target.name}", ())
        self._target = target

    def activate(self, commander):
        if not self._target.completed:
            raise RuntimeError(f"Cannot undo incomplete feature {self._target.name}")
        self._blocks = list(self._target.reversal_blocks())
        super().activate(commander)
