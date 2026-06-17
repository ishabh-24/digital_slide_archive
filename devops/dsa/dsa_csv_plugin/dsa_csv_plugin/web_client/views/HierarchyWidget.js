import { wrap } from '@girder/core/utilities/PluginUtils';
import HierarchyWidget from '@girder/core/views/widgets/HierarchyWidget';

/**
 * Add a "Filter Slides" button to the folder header in the Girder hierarchy
 * view. It opens the Browse & Filter page scoped to the current folder, so the
 * user can build a metadata-filtered subset of the slides in that folder.
 */
wrap(HierarchyWidget, 'render', function (render) {
    render.call(this);

    const parent = this.parentModel;
    // Only meaningful inside a folder (that is where slide items live).
    if (!parent || parent.resourceName !== 'folder') {
        return this;
    }

    const headerButtons = this.$('.g-folder-header-buttons');
    if (!headerButtons.length || headerButtons.find('.g-dsa-filter-slides').length) {
        return this;
    }

    const url = '/slidefilter?folderId=' + parent.id;
    headerButtons.append(
        '<a class="g-dsa-filter-slides btn btn-sm btn-info" ' +
        'href="' + url + '" target="_blank" rel="noopener" ' +
        'title="Browse and filter the slides in this folder by CSV metadata">' +
        '<i class="icon-search"></i>Filter Slides</a>'
    );

    return this;
});
