#pythonConsole.py
#A part of NonVisual Desktop Access (NVDA)
#This file is covered by the GNU General Public License.
#See the file COPYING for more details.
#Copyright (C) 2008-2018 NV Access Limited, Babbage B.V.

import watchdog

"""Provides an interactive Python console which can be run from within NVDA.
To use, call L{initialize} to create a singleton instance of the console GUI. This can then be accessed externally as L{consoleUI}.
"""

import __builtin__
import os
import code
import sys
import pydoc
import re
import itertools
import rlcompleter
import wx
from baseObject import AutoPropertyObject
import speech
import queueHandler
import api
import gui
from logHandler import log
import braille
import config

class HelpCommand(object):
	"""
	Emulation of the 'help' command found in the Python interactive shell.
	"""

	_reprMessage=_("Type help(object) to get help about object.")

	def __repr__(self):
		return self._reprMessage

	def __call__(self,*args,**kwargs):
		return pydoc.help(*args,**kwargs)

class ExitConsoleCommand(object):
	"""
	An object that can be used as an exit command that can close the console or print a friendly message for its repr.
	"""

	def __init__(self, exitFunc):
		self._exitFunc = exitFunc

	_reprMessage=_("Type exit() to exit the console")
	def __repr__(self):
		return self._reprMessage

	def __call__(self):
		self._exitFunc()

#: The singleton Python console UI instance.
consoleUI = None

class Completer(rlcompleter.Completer):

	def _callable_postfix(self, val, word):
		# Just because something is callable doesn't always mean we want to call it.
		return word

class PythonConsole(code.InteractiveConsole, AutoPropertyObject):
	"""An interactive Python console for NVDA which directs output to supplied functions.
	This is necessary for a Python console with input/output other than stdin/stdout/stderr.
	Input is always received via the L{push} method.
	This console handles redirection of stdout and stderr and prevents clobbering of the gettext "_" builtin.
	The console's namespace is populated with useful modules
	and can be updated with a snapshot of NVDA's state using L{updateNamespaceSnapshotVars}.
	"""

	def __init__(self, outputFunc, setPromptFunc, exitFunc, echoFunc=None, **kwargs):
		self._output = outputFunc
		self._echo = echoFunc
		self._setPrompt = setPromptFunc

		#: The namespace available to the console. This can be updated externally.
		#: @type: dict
		# Populate with useful modules.
		exitCmd = ExitConsoleCommand(exitFunc)
		self.namespace = {
			"help": HelpCommand(),
			"exit": exitCmd,
			"quit": exitCmd,
			"sys": sys,
			"os": os,
			"wx": wx,
			"log": log,
			"api": api,
			"queueHandler": queueHandler,
			"speech": speech,
			"braille": braille,
		}
		#: The variables last added to the namespace containing a snapshot of NVDA's state.
		#: @type: dict
		self._namespaceSnapshotVars = None

		# Can't use super here because stupid code.InteractiveConsole doesn't sub-class object. Grrr!
		code.InteractiveConsole.__init__(self, locals=self.namespace, **kwargs)
		self.prompt = ">>>"

	def _set_prompt(self, prompt):
		self._prompt = prompt
		self._setPrompt(prompt)

	def _get_prompt(self):
		return self._prompt

	def write(self, data):
		self._output(data)

	def push(self, line):
		if self._echo:
			self._echo("%s %s\n" % (self.prompt, line))
		# Capture stdout/stderr output as well as code interaction.
		stdout, stderr = sys.stdout, sys.stderr
		sys.stdout = sys.stderr = self
		# Prevent this from messing with the gettext "_" builtin.
		saved_ = __builtin__._
		more = code.InteractiveConsole.push(self, line)
		sys.stdout, sys.stderr = stdout, stderr
		__builtin__._ = saved_
		self.prompt = "..." if more else ">>>"
		return more

	def updateNamespaceSnapshotVars(self):
		"""Update the console namespace with a snapshot of NVDA's current state.
		This creates/updates variables for the current focus, navigator object, etc.
		"""
		self._namespaceSnapshotVars = {
			"focus": api.getFocusObject(),
			# Copy the focus ancestor list, as it gets mutated once it is replaced in api.setFocusObject.
			"focusAnc": list(api.getFocusAncestors()),
			"fdl": api.getFocusDifferenceLevel(),
			"caret": api.getCaretObject(),
			"fg": api.getForegroundObject(),
			"nav": api.getNavigatorObject(),
			"review":api.getReviewPosition(),
			"mouse": api.getMouseObject(),
			"brlRegions": braille.handler.buffer.regions,
		}
		self.namespace.update(self._namespaceSnapshotVars)

	def removeNamespaceSnapshotVars(self):
		"""Remove the variables from the console namespace containing the last snapshot of NVDA's state.
		This removes the variables added by L{updateNamespaceSnapshotVars}.
		"""
		if not self._namespaceSnapshotVars:
			return
		for key in self._namespaceSnapshotVars:
			try:
				del self.namespace[key]
			except KeyError:
				pass
		self._namespaceSnapshotVars = None

class ConsoleUI(wx.Frame):
	"""The NVDA Python console GUI.
	"""

	def __init__(self, parent):
		super(ConsoleUI, self).__init__(parent, wx.ID_ANY, _("NVDA Python Console"))
		self.Bind(wx.EVT_ACTIVATE, self.onActivate)
		self.Bind(wx.EVT_CLOSE, self.onClose)
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		self.outputCtrl = wx.TextCtrl(self, wx.ID_ANY, size=(500, 500), style=wx.TE_MULTILINE | wx.TE_READONLY|wx.TE_RICH)
		self.outputCtrl.Bind(wx.EVT_KEY_DOWN, self.onOutputKeyDown)
		self.outputCtrl.Bind(wx.EVT_CHAR, self.onOutputChar)
		mainSizer.Add(self.outputCtrl, proportion=2, flag=wx.EXPAND)
		inputSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.promptLabel = wx.StaticText(self, wx.ID_ANY)
		inputSizer.Add(self.promptLabel, flag=wx.EXPAND)
		self.inputCtrl = wx.TextCtrl(self, wx.ID_ANY, style=wx.TE_DONTWRAP | wx.TE_PROCESS_TAB)
		self.inputCtrl.Bind(wx.EVT_CHAR, self.onInputChar)
		inputSizer.Add(self.inputCtrl, proportion=1, flag=wx.EXPAND)
		mainSizer.Add(inputSizer, proportion=1, flag=wx.EXPAND)
		self.SetSizer(mainSizer)
		mainSizer.Fit(self)

		self.console = PythonConsole(outputFunc=self.output, echoFunc=self.echo, setPromptFunc=self.setPrompt, exitFunc=self.Close)
		self.completer = Completer(namespace=self.console.namespace)
		self.completionAmbiguous = False
		# Even the most recent line has a position in the history, so initialise with one blank line.
		self.inputHistory = [""]
		self.inputHistoryPos = 0

	def onActivate(self, evt):
		if evt.GetActive():
			self.inputCtrl.SetFocus()
		evt.Skip()

	def onClose(self, evt):
		self.Hide()
		self.console.removeNamespaceSnapshotVars()

	def output(self, data):
		self.outputCtrl.write(data)
		if data and not data.isspace():
			queueHandler.queueFunction(queueHandler.eventQueue, speech.speakText, data)

	def echo(self, data):
		self.outputCtrl.write(data)

	def setPrompt(self, prompt):
		self.promptLabel.SetLabel(prompt)
		queueHandler.queueFunction(queueHandler.eventQueue, speech.speakText, prompt)

	def execute(self):
		data = self.inputCtrl.GetValue()
		watchdog.alive()
		self.console.push(data)
		watchdog.asleep()
		if data:
			# Only add non-blank lines to history.
			if len(self.inputHistory) > 1 and self.inputHistory[-2] == data:
				# The previous line was the same and we don't want consecutive duplicates, so trash the most recent line.
				del self.inputHistory[-1]
			else:
				# Update the content for the most recent line of history.
				self.inputHistory[-1] = data
			# Start with a new, blank line.
			self.inputHistory.append("")
		self.inputHistoryPos = len(self.inputHistory) - 1
		self.inputCtrl.ChangeValue("")

	def historyMove(self, movement):
		newIndex = self.inputHistoryPos + movement
		if not (0 <= newIndex < len(self.inputHistory)):
			# No more lines in this direction.
			return False
		# Update the content of the history at the current position.
		self.inputHistory[self.inputHistoryPos] = self.inputCtrl.GetValue()
		self.inputHistoryPos = newIndex
		self.inputCtrl.ChangeValue(self.inputHistory[newIndex])
		self.inputCtrl.SetInsertionPointEnd()
		return True

	RE_COMPLETE_UNIT = re.compile(r"[\w.]*$")
	def complete(self):
		try:
			original = self.RE_COMPLETE_UNIT.search(self.inputCtrl.GetValue()).group(0)
		except AttributeError:
			return False

		completions = list(self._getCompletions(original))
		if self.completionAmbiguous:
			menu = wx.Menu()
			for comp in completions:
				# Only show text after the last dot (so as to not keep repeting the class or module in the context menu)
				label=comp.rsplit('.',1)[-1]
				item = menu.Append(wx.ID_ANY, label)
				self.Bind(wx.EVT_MENU,
					lambda evt, completion=comp: self._insertCompletion(original, completion),
					item)
			self.PopupMenu(menu)
			menu.Destroy()
			return True
		self.completionAmbiguous = len(completions) > 1

		completed = self._findBestCompletion(original, completions)
		if not completed:
			return False
		self._insertCompletion(original, completed)
		return not self.completionAmbiguous

	def _getCompletions(self, original):
		for state in itertools.count():
			completion = self.completer.complete(original, state)
			if not completion:
				break
			yield completion

	def _findBestCompletion(self, original, completions):
		if not completions:
			return None
		if len(completions) == 1:
			return completions[0]

		# Find the longest completion.
		longestComp = None
		longestCompLen = 0
		for comp in completions:
			compLen = len(comp)
			if compLen > longestCompLen:
				longestComp = comp
				longestCompLen = compLen
		# Find the longest common prefix.
		for prefixLen in xrange(longestCompLen, 0, -1):
			prefix = comp[:prefixLen]
			for comp in completions:
				if not comp.startswith(prefix):
					break
			else:
				# This prefix is common to all completions.
				if prefix == original:
					# We didn't actually complete anything.
					return None
				return prefix
		return None

	def _insertCompletion(self, original, completed):
		self.completionAmbiguous = False
		insert = completed[len(original):]
		if not insert:
			return
		self.inputCtrl.SetValue(self.inputCtrl.GetValue() + insert)
		queueHandler.queueFunction(queueHandler.eventQueue, speech.speakText, insert)
		self.inputCtrl.SetInsertionPointEnd()

	def onInputChar(self, evt):
		key = evt.GetKeyCode()

		if key == wx.WXK_TAB:
			line = self.inputCtrl.GetValue()
			if line and not line.isspace():
				if not self.complete():
					wx.Bell()
				return
		# This is something other than autocompletion, so reset autocompletion state.
		self.completionAmbiguous = False

		if key == wx.WXK_RETURN:
			self.execute()
			return
		elif key in (wx.WXK_UP, wx.WXK_DOWN):
			if self.historyMove(-1 if key == wx.WXK_UP else 1):
				return
		elif key == wx.WXK_F6:
			self.outputCtrl.SetFocus()
			return
		elif key == wx.WXK_ESCAPE:
			self.Close()
			return
		evt.Skip()

	def onOutputKeyDown(self, evt):
		key = evt.GetKeyCode()
		# #3763: WX 3 no longer passes escape to evt_char for richEdit fields, therefore evt_key_down is used.
		if key == wx.WXK_ESCAPE:
			self.Close()
			return
		evt.Skip()

	def onOutputChar(self, evt):
		key = evt.GetKeyCode()
		if key == wx.WXK_F6:
			self.inputCtrl.SetFocus()
			return
		evt.Skip()

def initialize():
	"""Initialize the NVDA Python console GUI.
	This creates a singleton instance of the console GUI. This is accessible as L{consoleUI}. This may be manipulated externally.
	"""
	global consoleUI
	consoleUI = ConsoleUI(gui.mainFrame)

def activate():
	"""Activate the console GUI.
	This shows the GUI and brings it to the foreground if possible.
	@precondition: L{initialize} has been called.
	"""
	global consoleUI
	consoleUI.Raise()
	# There is a MAXIMIZE style which can be used on the frame at construction, but it doesn't seem to work the first time it is shown,
	# probably because it was in the background.
	# Therefore, explicitly maximise it here.
	# This also ensures that it will be maximized whenever it is activated, even if the user restored/minimised it.
	consoleUI.Maximize()
	consoleUI.Show()
