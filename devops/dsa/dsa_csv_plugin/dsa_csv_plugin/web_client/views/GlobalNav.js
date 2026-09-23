import { wrap } from '@girder/core/utilities/PluginUtils';
import LayoutGlobalNavView from '@girder/core/views/layout/GlobalNavView';

/**
 * Add an "Annotation Tools" entry to Girder's top navigation. It opens the
 * plugin's landing page (upload & validate, convert, format specification)
 * so none of those pages need a typed URL.
 *
 * The link deliberately does not carry Girder's `g-nav-link` class: that
 * class is bound to an in-app router handler, and this is a plain page.
 */
wrap(LayoutGlobalNavView, 'render', function (render) {
    render.call(this);

    const list = this.$('ul.g-global-nav');
    if (!list.length || list.find('.g-dsa-annotation-tools').length) {
        return this;
    }

    list.append(
        '<li class="g-global-nav-li">' +
        '<a class="g-dsa-annotation-tools" href="/annotation_tools" ' +
        'title="Upload, validate and convert slide annotations; format specification">' +
        '<i class="icon-tags"></i><span>Annotation Tools</span></a></li>'
    );

    return this;
});
