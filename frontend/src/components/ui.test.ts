import { createElement, type ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  Label,
  Modal,
  Select,
  Spinner,
  Textarea,
} from './ui';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const props = (node: ReactNode): any => (node as any).props;

// This file is .ts (not .tsx), so Modal — the only component here using
// hooks — is rendered via createElement rather than JSX. Calling Modal({...})
// directly as a plain function (as every other component in this file does,
// via structural inspection of the returned element) breaks its useEffect:
// hooks only work inside an actual React render pass.
const modal = (props: Parameters<typeof Modal>[0]) => createElement(Modal, props);

describe('Button', () => {
  it('defaults to variant=default, size=md', () => {
    const el = Button({ children: 'Save' });
    expect(props(el).className).toContain('bg-primary');
    expect(props(el).className).toContain('h-9');
    expect(props(el).children).toBe('Save');
  });

  it('applies the outline variant and sm size', () => {
    const el = Button({ children: 'Cancel', variant: 'outline', size: 'sm' });
    expect(props(el).className).toContain('border border-border');
    expect(props(el).className).toContain('h-8');
  });

  it('applies the ghost and destructive variants', () => {
    expect(props(Button({ children: 'x', variant: 'ghost' })).className).toContain('bg-transparent');
    expect(props(Button({ children: 'x', variant: 'destructive' })).className).toContain('bg-destructive');
  });

  it('merges a custom className and forwards extra props', () => {
    const onClick = () => {};
    const el = Button({ children: 'x', className: 'my-btn', onClick, disabled: true });
    expect(props(el).className).toContain('my-btn');
    expect(props(el).onClick).toBe(onClick);
    expect(props(el).disabled).toBe(true);
  });
});

describe('Input', () => {
  it('renders an input with the base classes and forwards props', () => {
    const onChange = () => {};
    const el = Input({ value: 'x', onChange, placeholder: 'Search' });
    expect(props(el).className).toContain('h-9');
    expect(props(el).value).toBe('x');
    expect(props(el).placeholder).toBe('Search');
  });

  it('merges a custom className', () => {
    const el = Input({ className: 'w-32' });
    expect(props(el).className).toContain('w-32');
  });
});

describe('Textarea', () => {
  it('renders a textarea with the base classes and forwards props', () => {
    const el = Textarea({ value: 'notes', rows: 4 });
    expect(props(el).className).toContain('rounded-md');
    expect(props(el).value).toBe('notes');
    expect(props(el).rows).toBe(4);
  });

  it('merges a custom className', () => {
    const el = Textarea({ className: 'h-40' });
    expect(props(el).className).toContain('h-40');
  });
});

describe('Select', () => {
  it('renders a select with children and forwards props', () => {
    const child = 'option-a';
    const el = Select({ children: child, value: 'a', onChange: () => {} });
    expect(props(el).children).toBe(child);
    expect(props(el).value).toBe('a');
  });

  it('merges a custom className', () => {
    const el = Select({ children: null, className: 'w-24' });
    expect(props(el).className).toContain('w-24');
  });
});

describe('Label', () => {
  it('renders children and htmlFor', () => {
    const el = Label({ children: 'Campaign name', htmlFor: 'campaign-name' });
    expect(props(el).children).toBe('Campaign name');
    expect(props(el).htmlFor).toBe('campaign-name');
  });

  it('merges a custom className', () => {
    const el = Label({ children: 'x', className: 'text-red-500' });
    expect(props(el).className).toContain('text-red-500');
  });
});

describe('Card', () => {
  it('renders children inside the card container', () => {
    const el = Card({ children: 'card body' });
    expect(props(el).children).toBe('card body');
    expect(props(el).className).toContain('rounded-lg');
  });

  it('merges a custom className', () => {
    const el = Card({ children: 'x', className: 'p-8' });
    expect(props(el).className).toContain('p-8');
  });
});

describe('Badge', () => {
  it('defaults to the default tone', () => {
    const el = Badge({ children: 'Draft' });
    expect(props(el).className).toContain('bg-muted');
    expect(props(el).children).toBe('Draft');
  });

  it('applies the success/warning/danger/muted tones', () => {
    expect(props(Badge({ children: 'x', tone: 'success' })).className).toContain('bg-green-100');
    expect(props(Badge({ children: 'x', tone: 'warning' })).className).toContain('bg-amber-100');
    expect(props(Badge({ children: 'x', tone: 'danger' })).className).toContain('bg-red-100');
    expect(props(Badge({ children: 'x', tone: 'muted' })).className).toContain('text-muted-foreground');
  });
});

describe('Spinner', () => {
  it('renders a decorative spinning span', () => {
    const el = Spinner();
    expect(props(el)['aria-hidden']).toBe(true);
    expect(props(el).className).toContain('animate-spin');
  });
});

describe('Field', () => {
  it('renders the label and children, no hint by default', () => {
    const el = Field({ label: 'Segment', htmlFor: 'segment', children: 'CHILD' });
    const [labelEl, children, hint] = props(el).children;
    expect(props(labelEl).children).toBe('Segment');
    expect(children).toBe('CHILD');
    expect(hint).toBeNull();
  });

  it('renders a muted hint by default when provided', () => {
    const el = Field({ label: 'Segment', children: 'CHILD', hint: 'Helper text' });
    const [, , hintP] = props(el).children;
    expect(props(hintP).children).toBe('Helper text');
    expect(props(hintP).className).toContain('text-muted-foreground');
  });

  it('renders a danger-toned hint when hintTone="danger"', () => {
    const el = Field({ label: 'Segment', children: 'CHILD', hint: 'Required', hintTone: 'danger' });
    const [, , hintP] = props(el).children;
    expect(props(hintP).className).toContain('text-destructive');
  });
});

describe('Modal', () => {
  const noop = () => {};

  it('renders nothing when closed', () => {
    render(modal({ open: false, onClose: noop, title: 'T', children: 'C' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('renders a portal into document.body when open, with dialog semantics', () => {
    render(modal({ open: true, onClose: noop, title: 'Enable Campaign', children: 'Body' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(dialog.parentElement).toBe(document.body);
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAttribute('aria-label', 'Enable Campaign');
  });

  it('clicking the overlay calls onClose; clicking the inner panel stops propagation', async () => {
    const onClose = vi.fn();
    render(modal({ open: true, onClose, title: 'T', children: 'C' }));
    const user = userEvent.setup();

    await user.click(screen.getByText('C'));
    expect(onClose).not.toHaveBeenCalled();

    await user.click(screen.getByRole('dialog'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('defaults maxWidth to max-w-2xl and applies a custom maxWidth', () => {
    const { unmount } = render(modal({ open: true, onClose: noop, title: 'T', children: 'C' }));
    expect(screen.getByRole('dialog').firstElementChild).toHaveClass('max-w-2xl');
    unmount();

    render(
      modal({ open: true, onClose: noop, title: 'T', children: 'C', maxWidth: 'max-w-5xl' }),
    );
    expect(screen.getByRole('dialog').firstElementChild).toHaveClass('max-w-5xl');
  });

  it('renders the title and a close button that calls onClose', async () => {
    const onClose = vi.fn();
    render(modal({ open: true, onClose, title: 'My Title', children: 'C' }));
    expect(screen.getByText('My Title')).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('renders children in the body region', () => {
    render(modal({ open: true, onClose: noop, title: 'T', children: 'The body content' }));
    expect(screen.getByText('The body content')).toBeInTheDocument();
  });

  it('omits the footer region when no footer is provided', () => {
    render(modal({ open: true, onClose: noop, title: 'T', children: 'C' }));
    expect(document.querySelector('footer')).not.toBeInTheDocument();
  });

  it('renders the footer region when a footer is provided', () => {
    render(
      modal({ open: true, onClose: noop, title: 'T', children: 'C', footer: 'Footer buttons' }),
    );
    expect(screen.getByText('Footer buttons').closest('footer')).toBeInTheDocument();
  });

  it('calls onClose on Escape only', () => {
    const onClose = vi.fn();
    render(modal({ open: true, onClose, title: 'T', children: 'C' }));

    fireEvent.keyDown(document, { key: 'Enter' });
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('unmounting removes the keydown listener (Escape no longer calls onClose)', () => {
    const onClose = vi.fn();
    const { unmount } = render(modal({ open: true, onClose, title: 'T', children: 'C' }));
    unmount();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });
});
